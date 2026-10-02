"""Primitive 7 & Stage 1: Bidirectional Drift Adoption & Workspace Sync.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 5: Primitive Entry Point & Orchestration
    run_primitive_adopt_drifts(workspace_config, package_names, interactive, accept_conflicts, force, dry_run, flags)
        1. Planning:
            plan_adopt_repo [Layer 5] -> get_package_drifts [Layer 1]
        2. Dry-Run Reporting (if dry_run=True):
            dry_run_adopt [Layer 3]
        3. Execution:
            execute_adopt_repo [Layer 5] -> execute_package_adopt [Layer 4]
        4. State Synchronization & Commit:
            commit_staged_repo_changes (commits only staged adopted paths in install/ repo)

    plan_adopt_repo(workspace_config, package_names) -> AdoptPlan
        Builds a multi-package AdoptPlan containing PackageAdoptPlans.

    execute_adopt_repo(workspace_config, plan, interactive, accept_conflicts, force, flags) -> AdoptResult
        Executes adoption across packages, stages resolved files, and commits staged changes in install/ repo.

Layer 4: Single-Package Drift Adoption & Planning
    adopt_one_package_drifts(workspace_config, pkg, interactive, accept_conflicts, force, dry_run, flags)
        Coordinates single-package planning and execution.
        NOTE: adopt_one_package_drifts does NOT commit the install/ repo; it only reconciles source files
        and stages adopted paths in the install/ index. Committing staged changes is performed at Layer 5
        orchestration (execute_adopt_repo / commit_staged_repo_changes) or by the caller.

    plan_package_adopt(workspace_config, pkg) -> PackageAdoptPlan
        Convenience alias for get_package_drifts [Layer 1].

    execute_package_adopt(workspace_config, plan, interactive, accept_conflicts, force, flags) -> PackageAdoptResult
        1. Pre-Source Hook:
            trigger_pre_source_hook
        2. Process Drift Categories (Additions, Deletions, Renames, Modifications):
            handle_single_addition [Layer 3]
            handle_single_deletion [Layer 3]
            handle_single_rename [Layer 3] (reads pre-computed patch)
            handle_single_modification [Layer 3] (reads pre-computed patch)
        3. Staging Resolved Files:
            git -C <install_path> add -- <resolved_paths>
            NOTE: Only successfully adopted files are staged. Does NOT commit the install/ repo.

Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
    handle_single_addition
        Verifies target file cleanliness, evaluates collisions with existing source files, and delegates to adopt_addition / ignore_addition / discard / skip.
    handle_single_deletion
        Verifies target file cleanliness, validates target presence in source, and delegates to adopt_deletion / discard / skip.
    handle_single_rename -> handle_rename_non_interactive / handle_rename_interactive
        Verifies old source file cleanliness, generates adjusted patch headers, evaluates conflict status, and routes to adopt_rename / freeze / merge editor / side-by-side.
    handle_single_modification -> handle_modification_non_interactive / handle_modification_interactive
        Verifies target source file cleanliness, evaluates template vs. static modifications, checks patch conflicts, logs/prints diffs, and routes to patch_and_edit / freeze / side-by-side.

Layer 2: Single-File Reconciliation Actions
    _sync_file_mode(src_file, install_file)
        Synchronizes file permissions and executable bits from install/ onto src_file.
    adopt_addition(pkg_dir, install_pkg_dir, rel_path)
        Copies a wild host-side added file into the declarative source folder under src/.
    ignore_addition(pkg_dir, install_pkg_dir, rel_path)
        Unlinks file from install/ base, un-tracks it in install/ Git index, and registers pattern into .drift_ignore.
    adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg)
        Symmetrically deletes the matching source template/file from declarative source directory.
    patch_and_edit(src_file, patch_content, install_file, accept_conflicts, open_editor)
        Unified patch application engine: applies unified content diffs (invoking patch only when '@@' diff hunks exist),
        synchronizes file mode/permissions from install_file via _sync_file_mode, and optionally opens $EDITOR.
    adopt_rename(render_engines, src_dir_to_render, old_rel_path, new_rel_path, patch_content, has_patch_conflict, install_file, accept_conflicts)
        Symmetrically renames template files in src/ using copy-and-patch with atomic cleanup, applies content patches,
        synchronizes permissions, and returns the resolved new_src_file Path.
    fallback_over_render(src_file, static_file)
        Backs up original template to .bak and overwrites it with static content from install/ (freezing template).
    fallback_side_by_side(src_file, install_file)
        Launches visual side-by-side diff in $EDITOR (nvim, vim, code, emacs) between source template and live static drift.

Layer 1: Inspection & Git Patch Primitives
    get_drifted_packages
        Parses git status porcelain across install/ to discover all packages with local host modifications.
    assert_source_file_clean
        Enforces scoped Git cleanliness safeguard on target source file before applying modifications.
    get_package_drifts
        Categorizes Git porcelain status into additions, deletions, modifications, and renames.
    generate_unified_patch
        Executes git diff HEAD to extract raw unified diff patches for drifted files in install/.
    generate_adjusted_patch
        Rewrites unified diff headers to align install/ file paths with target template paths in src/.
    check_patch_conflicts
        Runs patch --dry-run for patches containing '@@' diff hunks (returns False early for hunkless/mode-only diffs).
    apply_source_patch
        Executes patch tool against source files for patches containing '@@' diff hunks with optional --merge conflict support.
    test_file_conflict
        Convenience wrapper checking patch conflicts for a single modified file.
    resolve_source_file_path
        Resolves physical source template paths using RenderEngineRegistry suffix mapping.
-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Inspection & Git Patch Primitives
    Layer 2: Single-File Reconciliation Actions
    Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
    Layer 4: Single-Package Drift Adoption
    Layer 5: Public Primitive Entry Point
===============================================================================
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Sequence, Union

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from ..config.render_engine_config import RenderEngineRegistry
from ..core.constants import DRIFT_IGNORE_FILE_NAME, DRIFT_KEEP_FILE_NAME
from ..core.result_models import AdoptPlan, AdoptResult, PackageAdoptPlan, PackageAdoptResult
from ..utils.git_utils import (
    get_git_status_porcelain,
    parse_git_status_porcelain,
    has_uncommitted_modifications,
    get_drift_root,
    commit_staged_repo_changes,
)
from ..utils.file_ops import remove_file_or_empty_dir, remove_tree, copy_file, ensure_dir
from ..hooks.lifecycle_hooks import HookExecFlags, trigger_pre_source_hook
from ..utils.editor_utils import launch_single_file_editor, launch_side_by_side_editor

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Inspection & Git Patch Primitives
# =====================================================================

def get_drifted_packages(workspace_config: WorkspaceConfig) -> List[str]:
    """Scans the install/ repository status to identify which packages have uncommitted drifts."""
    lines = get_git_status_porcelain(workspace_config.install_path)
    changed_names = set()
    for line in lines:
        if len(line) < 4:
            continue
        path_str = line[3:].strip()
        parts = Path(path_str).parts
        if parts:
            changed_names.add(parts[0])

    installed_packages = set(workspace_config.get_package_names_from_dir(workspace_config.install_path))
    source_packages = set(workspace_config.get_package_names_from_source_dir())

    drifted = (changed_names & installed_packages) & source_packages
    return sorted(drifted)


def assert_source_file_clean(
    src_file: Path,
    pkg: str,
    force: bool = False
) -> None:
    """Verifies that a target source file is clean before adopting drifts into it."""
    if not src_file.exists():
        return

    try:
        drift_root = get_drift_root(src_file.parent)
    except Exception:
        drift_root = src_file.parent

    if not has_uncommitted_modifications(drift_root, src_file):
        return

    if force:
        logger.warning(
            f"⚠️  [FORCE] Bypassing Git cleanliness safeguard for '{src_file.name}'. Overwriting uncommitted modifications."
        )
        return

    try:
        rel_dirty = src_file.relative_to(drift_root)
    except ValueError:
        rel_dirty = src_file

    raise RuntimeError(
        f"The source file '{rel_dirty}' has uncommitted local modifications!\n"
        f"Adopting system drift would overwrite uncommitted changes in this file.\n\n"
        f"👉 Run 'drift adopt {pkg} --force' (or -f) to overwrite existing file modifications.\n"
        f"👉 Or commit / stash your changes before adopting."
    )



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


def check_patch_conflicts(src_file: Path, patch_content: str) -> bool:
    """Runs a dry-run of patch to determine if there are conflicts applying the patch to src_file.

    Note: The external patch tool (especially BSD patch on macOS) requires diff hunk blocks
    (lines starting with '@@') and errors if passed a hunkless diff (e.g. mode-only git diffs).
    If patch_content lacks '@@' hunks, returns False (no conflicts) immediately without calling patch.
    """
    if not any(line.startswith("@@") for line in patch_content.splitlines()):
        return False
    cmd = ["patch", "--dry-run", "--no-backup-if-mismatch", str(src_file)]
    try:
        subprocess.run(cmd, input=patch_content, capture_output=True, text=True, check=True)
        return False
    except subprocess.CalledProcessError:
        return True


def apply_source_patch(src_file: Path, patch_content: str, accept_conflicts: bool = False) -> bool:
    """Applies a patch to a source file, supporting merge markers if accept_conflicts is True.

    Note: The external patch command operates strictly on text diff hunks ('@@' blocks).
    If patch_content has no '@@' hunks (e.g. file mode/permission-only drift), this function
    returns True immediately without executing the external patch binary, allowing file permissions
    to be applied via _sync_file_mode.
    """
    if not any(line.startswith("@@") for line in patch_content.splitlines()):
        return True
    cmd = ["patch", "--no-backup-if-mismatch"]
    if accept_conflicts:
        cmd.append("--merge")
    cmd.append(str(src_file))
    
    try:
        subprocess.run(cmd, input=patch_content, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False
    finally:
        rej_file = src_file.with_suffix(src_file.suffix + ".rej")
        if rej_file.exists():
            rej_file.unlink()


def test_file_conflict(src_file: Path, install_file: Path, install_base: Path, pkg_rel_path: Path) -> bool:
    """Evaluates patch conflicts for a single modified file against its '@@' diff hunks."""
    patch_content = generate_unified_patch(install_base, pkg_rel_path)
    return check_patch_conflicts(src_file, patch_content)


def resolve_source_file_path(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path
) -> Optional[Path]:
    """Resolves the physical file path inside src_dir_to_render using find_source_file_for_rendered_names."""
    match_info = render_engines.find_source_file_for_rendered_names(
        src_dir_to_render / rel_path.parent,
        [rel_path.name]
    )
    if match_info:
        return match_info.path
    return None


def get_package_drifts(
    workspace_config: WorkspaceConfig,
    pkg: str,
) -> PackageAdoptPlan:
    """Discovers and structures all system drifts for a single package into a PackageAdoptPlan.

    Temporarily stages the package in the install/ repository under a try...finally guard to allow
    Git rename detection and unified patch pre-computation, immediately restoring the staged index
    so that the working index remains untouched until explicit execution.
    """
    install_base = workspace_config.install_path
    src_pkg_dir = workspace_config.source_path / pkg
    render_engines = workspace_config.render_engine_configs
    try:
        pkg_config = PackageConfig.from_source_dir(src_pkg_dir, workspace_config)
        src_dir_to_render = pkg_config.get_source_directory_to_render(src_pkg_dir)
        render_engines = pkg_config.package_render_engines(workspace_config)
    except Exception:
        src_dir_to_render = src_pkg_dir

    install_pkg_dir = install_base / pkg

    # Pre-stage all changes in install repo under pkg subdirectory for rename detection
    res_add = subprocess.run(
        ["git", "-C", str(install_base), "add", "--all", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_add.returncode != 0:
        logger.warning(
            f"Pre-staging install changes for '{pkg}' exited with code {res_add.returncode}: {res_add.stderr.strip()}"
        )

    patches: Dict[str, str] = {}
    try:
        diff = parse_git_status_porcelain(install_base, pkg)
        raw_renames = sorted([(r.old_path, r.new_path) for r in diff.renamed], key=lambda x: x[1])
        renames: List[Tuple[Path, Path]] = []
        raw_additions = list(diff.added)
        raw_deletions = list(diff.deleted)

        # 1. Split renames involving .drift_keep
        for old_rel, new_rel in raw_renames:
            if old_rel.name == DRIFT_KEEP_FILE_NAME or new_rel.name == DRIFT_KEEP_FILE_NAME:
                raw_deletions.append(old_rel)
                raw_additions.append(new_rel)
            else:
                renames.append((old_rel, new_rel))

        # 2. Sanitize modifications and non-empty keep files
        modifications: List[Path] = []
        for rel_mod in diff.modified:
            if rel_mod.name == DRIFT_KEEP_FILE_NAME:
                logger.warning(
                    f"⚠️  [CORRUPT] Unexpected modification of keep file '{rel_mod}'. "
                    f"'.drift_keep' must always be an empty stub file."
                )
                continue
            modifications.append(rel_mod)

        for rel_add in raw_additions:
            if rel_add.name == DRIFT_KEEP_FILE_NAME:
                keep_path = install_pkg_dir / rel_add
                if keep_path.is_file() and keep_path.stat().st_size > 0:
                    logger.warning(
                        f"⚠️  [CORRUPT] Keep file '{rel_add}' is not empty (size={keep_path.stat().st_size} bytes). "
                        f"'.drift_keep' must always be 0 bytes."
                    )

        # 3. Translate .drift_keep additions and deletions to parent directories
        def _translate_keep_to_dir(p: Path) -> Path:
            return p.parent if p.name == DRIFT_KEEP_FILE_NAME and p.parent != Path(".") else p

        additions = sorted(list({_translate_keep_to_dir(p) for p in raw_additions}))
        deletions = sorted(list({_translate_keep_to_dir(p) for p in raw_deletions}))

        # Pre-compute unified diffs while package is staged in index
        for rel_mod in modifications:
            pkg_rel_path = Path(pkg) / rel_mod
            patch_content = generate_unified_patch(install_base, pkg_rel_path)
            patches[str(rel_mod)] = patch_content

        for old_rel, new_rel in renames:
            old_src_file = resolve_source_file_path(render_engines, src_dir_to_render, old_rel)
            target_name = old_src_file.name if old_src_file else old_rel.name
            adj_patch = generate_adjusted_patch(
                install_base=install_base,
                pkg=pkg,
                new_rel_path=new_rel,
                old_rel_path=old_rel,
                target_src_filename=target_name,
            )
            patches[f"{old_rel}->{new_rel}"] = adj_patch
    finally:
        # Guarantee unstage in install/ repo so working index remains clean
        res_rst = subprocess.run(
            ["git", "-C", str(install_base), "restore", "--staged", "--", pkg],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_rst.returncode != 0:
            logger.debug(
                f"Unstaging install changes for '{pkg}' exited with code {res_rst.returncode}: {res_rst.stderr.strip()}"
            )

    return PackageAdoptPlan(
        package=pkg,
        src_pkg_dir=src_pkg_dir,
        src_dir_to_render=src_dir_to_render,
        install_pkg_dir=install_pkg_dir,
        render_engines=render_engines,
        additions=additions,
        deletions=deletions,
        modifications=modifications,
        renames=renames,
        patches=patches,
        status="PENDING",
    )


plan_package_adopt = get_package_drifts


# =====================================================================
# Layer 2: Single-File Reconciliation Actions
# =====================================================================

def _sync_file_mode(src_file: Path, install_file: Path) -> None:
    """Synchronizes file mode/permissions from install_file onto src_file."""
    try:
        src_file.chmod(install_file.stat().st_mode)
    except Exception as e:
        logger.warning(f"Failed to synchronize file permissions from '{install_file}' to '{src_file}': {e}")


def adopt_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Copies a wild host-side added file or creates an empty directory in the declarative source folder."""
    dest = pkg_dir / rel_path
    src_install = install_pkg_dir / rel_path
    if src_install.is_dir():
        ensure_dir(dest)
    else:
        copy_file(src_install, dest)


def ignore_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Unlinks the file/directory from install base and registers pattern in .drift_ignore."""
    install_item = install_pkg_dir / rel_path
    if install_item.is_dir() and not install_item.is_symlink():
        remove_tree(install_item)
    elif install_item.exists() or install_item.is_symlink():
        remove_file_or_empty_dir(install_item)

    install_base = install_pkg_dir.parent
    rel_install_base = Path(install_pkg_dir.name) / rel_path
    # check=False is intentional: file may not be tracked in the git index yet.
    res_rm = subprocess.run(
        ["git", "-C", str(install_base), "rm", "-r", "--cached", "-f", "--", str(rel_install_base)],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_rm.returncode != 0:
        logger.debug(f"git rm --cached exited with code {res_rm.returncode} for '{rel_install_base}': {res_rm.stderr.strip()}")

    # Append pattern to .drift_ignore
    ignore_file = pkg_dir / DRIFT_IGNORE_FILE_NAME
    pattern = (rel_path.as_posix() + "/") if install_item.is_dir() else rel_path.as_posix()
    with ignore_file.open("a", encoding="utf-8") as f:
        f.write(f"\n{pattern}\n")


def adopt_deletion(render_engines: RenderEngineRegistry, src_dir_to_render: Path, rel_path: Path, pkg: str) -> None:
    """Symmetrically deletes the corresponding file or directory from declarative source folder."""
    src_dir = src_dir_to_render / rel_path
    if src_dir.is_dir() and not src_dir.is_symlink():
        remove_tree(src_dir)
        return
    src_file = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if src_file and (src_file.exists() or src_file.is_symlink()):
        remove_file_or_empty_dir(src_file)


def patch_and_edit(
    src_file: Path,
    patch_content: str,
    install_file: Optional[Path] = None,
    accept_conflicts: bool = False,
    open_editor: bool = False
) -> bool:
    """Applies a patch (clean or with merge markers) to a source file, synchronizes file permissions, and optionally opens $EDITOR.

    Note: The external patch tool is executed via apply_source_patch only when '@@' diff hunks exist.
    File mode/permissions are synchronized separately via _sync_file_mode when install_file is provided.
    """
    if patch_content.strip():
        success = apply_source_patch(src_file, patch_content, accept_conflicts=accept_conflicts)
        if not success:
            logger.error(f"Failed to apply patch to template file {src_file.name}. Please check manually.")
            return False
    if install_file is not None and install_file.exists():
        _sync_file_mode(src_file, install_file)
    if open_editor:
        try:
            launch_single_file_editor(src_file)
        except RuntimeError as e:
            logger.warning(f"⚠️  Failed to open editor: {e}. Skipping file adoption.")
            return False
    return True


def adopt_rename(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: str,
    has_patch_conflict: bool = False,
    install_file: Optional[Path] = None,
    accept_conflicts: bool = False
) -> Optional[Path]:
    """Symmetrically renames the source template file and applies any content patch."""
    old_src_file = resolve_source_file_path(render_engines, src_dir_to_render, old_rel_path)
    file_name = old_src_file.name if old_src_file else old_rel_path.name

    if has_patch_conflict:
        if accept_conflicts:
            logger.warning(f"⚠️  Applying conflicting patch into renamed template file: '{file_name}'")
        else:
            logger.error(f"❌ [CONFLICT] Cannot apply system diff cleanly onto renamed template file '{file_name}'. Skipping.")
            logger.error("   Run 'drift adopt --interactive' or pass '--accept-conflicts' to resolve.")
            return None

    if old_src_file and old_src_file.exists():
        new_src_name = render_engines.make_new_template_name(old_src_file.name,
                                                             new_rel_path.name)
        new_src_file = src_dir_to_render / new_rel_path.parent / new_src_name

        new_src_file.parent.mkdir(parents=True, exist_ok=True)
        copy_file(old_src_file, new_src_file)
    else:
        logger.warning(f"⚠️  Old source file for '{old_rel_path}' not found in source directory. Creating a new template file for '{new_rel_path}'.")
        new_src_file = src_dir_to_render / new_rel_path
        new_src_file.parent.mkdir(parents=True, exist_ok=True)
        new_src_file.touch()

    success = patch_and_edit(
        new_src_file,
        patch_content,
        install_file=install_file,
        accept_conflicts=accept_conflicts,
        open_editor=False
    )
    if not success:
        new_src_file.unlink(missing_ok=True)
        return None

    if old_src_file and old_src_file.exists() and old_src_file != new_src_file:
        old_src_file.unlink(missing_ok=True)

    return new_src_file


def fallback_over_render(src_file: Path, static_file: Path) -> None:
    """Backs up the original template to .bak and overwrites it with static file content (freezing template)."""
    bak_file = src_file.with_suffix(src_file.suffix + ".bak")
    copy_file(src_file, bak_file)
    copy_file(static_file, src_file)
    logger.warning(f"⚠️  [FREEZE] Overwrote template '{src_file.name}' with static content. Original template backed up to '{bak_file.name}'.")


def fallback_side_by_side(src_file: Path, install_file: Path) -> bool:
    """Launches editor to display both template and the final compiled/static drift as side-by-side reference."""
    try:
        launch_side_by_side_editor([(src_file, install_file)])
        return True
    except RuntimeError as e:
        logger.warning(f"⚠️  Failed to open side-by-side editor: {e}. Skipping file adoption.")
        return False


# =====================================================================
# Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
# =====================================================================

def dry_run_adopt(plan: PackageAdoptPlan) -> None:
    """Prints a clear preview of all drift changes and potential conflicts."""
    logger.info(f"\n🔍 [DRY RUN] Previewing drift adoption for package '{plan.package}':")

    install_base = plan.install_pkg_dir.parent

    if plan.additions:
        logger.info("   [+] Additions (will be copied to source):")
        for item in plan.additions:
            tag = " (directory)" if (plan.install_pkg_dir / item).is_dir() else ""
            logger.info(f"       + {item}{tag}")

    if plan.deletions:
        logger.info("   [-] Deletions (will be removed from source):")
        for item in plan.deletions:
            tag = " (directory)" if (plan.src_dir_to_render / item).is_dir() else ""
            logger.info(f"       - {item}{tag}")

    if plan.renames:
        logger.info("   [R] Renames (will rename the template in source):")
        for old_file, new_file in plan.renames:
            logger.info(f"       R {old_file} -> {new_file}")

    if plan.modifications:
        logger.info("   [~] Modifications:")
        for file in plan.modifications:
            src_file = resolve_source_file_path(plan.render_engines, plan.src_dir_to_render, file)
            if not src_file:
                logger.info(f"       ~ {file} [Static Overwrite]")
                continue

            # If the resolved source file is templated, check for conflicts
            is_templated = src_file.suffix in [".sh", ".toml", ".json", ".conf"] or ".envst" in src_file.name or ".mustache" in src_file.name
            if is_templated:
                patch_content = plan.patches.get(str(file))
                if patch_content is None:
                    install_file = plan.install_pkg_dir / file
                    pkg_rel_path = Path(plan.package) / file
                    has_conflict = test_file_conflict(src_file, install_file, install_base, pkg_rel_path)
                else:
                    has_conflict = check_patch_conflicts(src_file, patch_content)
                if has_conflict:
                    logger.info(f"       ~ {file} [CONFLICTS with template: {src_file.name}]")
                else:
                    logger.info(f"       ~ {file} [Patch applies cleanly to: {src_file.name}]")
            else:
                logger.info(f"       ~ {file} [Static Overwrite of: {src_file.name}]")


def _prompt_addition_collision_interactive(rel_path: Path, incoming_is_dir: bool) -> bool:
    """Prompts the user when an addition collides with an existing file/directory in source."""
    incoming_type = "directory" if incoming_is_dir else "file"
    existing_type = "file" if incoming_is_dir else "directory"
    print(f"\n⚠️  [CONFLICT] Cannot adopt {incoming_type} '{rel_path}' because a {existing_type} with the same name exists in source!")
    print("Reconciliation options:")
    print(f"[1] Discard addition / Restore (removes {incoming_type} on host next deployment)")
    print(f"[2] Skip {incoming_type}")
    choice = input("Select option [1-2]: ").strip()
    return choice == "1"


def _prompt_directory_addition_interactive(
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
) -> bool:
    """Interactive menu for adopting an untracked directory addition."""
    print(f"\nFound untracked directory addition inside Fully-Controlled Directory: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt directory into source package (creates empty directory)")
    print("[2] Ignore directory (appends pattern to package .drift_ignore)")
    print("[3] Discard directory (removes directory on host next deployment)")
    print("[4] Skip directory")
    choice = input("Select option [1-4]: ").strip()
    if choice == "1":
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return True
    elif choice == "2":
        ignore_addition(src_pkg_dir, install_pkg_dir, rel_path)
        return True
    elif choice == "3":
        return True
    else:
        return False


def _prompt_file_addition_interactive(
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
) -> bool:
    """Interactive menu for adopting an untracked file addition."""
    print(f"\nFound untracked file addition inside Fully-Controlled Directory: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt and copy into source package")
    print("[2] Ignore file (appends pattern to package .drift_ignore)")
    print("[3] Discard file (stages file to install/ database so it is deleted on next deploy)")
    print("[4] Skip file")
    choice = input("Select option [1-4]: ").strip()
    if choice == "1":
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return True
    elif choice == "2":
        ignore_addition(src_pkg_dir, install_pkg_dir, rel_path)
        return True
    elif choice == "3":
        return True
    else:
        return False


def _handle_single_directory_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> bool:
    """Handles adoption of an empty directory addition."""
    # 1. Pre-existing directory in source: already matches desired state, skip cleanly
    if (src_dir_to_render / rel_path).is_dir():
        logger.info(f"Directory '{rel_path}' already exists in source. Skipping addition.")
        return True

    # 2. File / template collision check
    if (src_dir_to_render / rel_path).is_file() or resolve_source_file_path(render_engines, src_dir_to_render, rel_path) is not None:
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot adopt directory '{rel_path}' because a file already exists at that path in source. Skipping.")
            return False
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=True)

    # 3. Clean directory addition
    assert_source_file_clean(src_dir_to_render / rel_path, force=force, pkg=install_pkg_dir.name)
    if not interactive:
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return True
    return _prompt_directory_addition_interactive(src_pkg_dir, src_dir_to_render, install_pkg_dir, rel_path)


def _handle_single_file_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> bool:
    """Handles adoption of a regular file addition."""
    # 1. Directory collision check
    if (src_dir_to_render / rel_path).is_dir():
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot adopt file '{rel_path}' because a directory already exists at that path in source. Skipping.")
            return False
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=False)

    # 2. Existing file collision check
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if target_existing_src is not None:
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot adopt addition '{rel_path}' because the target already exists in source. Skipping.")
            return False
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=False)

    # 3. Clean file addition
    target_src_file = src_dir_to_render / rel_path
    assert_source_file_clean(target_src_file, force=force, pkg=install_pkg_dir.name)
    if not interactive:
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return True
    return _prompt_file_addition_interactive(src_pkg_dir, src_dir_to_render, install_pkg_dir, rel_path)


def handle_single_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> bool:
    """Handles drift reconciliation for a single file or directory addition."""
    if (install_pkg_dir / rel_path).is_dir():
        return _handle_single_directory_addition(
            render_engines=render_engines,
            src_pkg_dir=src_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            install_pkg_dir=install_pkg_dir,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        )
    return _handle_single_file_addition(
        render_engines=render_engines,
        src_pkg_dir=src_pkg_dir,
        src_dir_to_render=src_dir_to_render,
        install_pkg_dir=install_pkg_dir,
        rel_path=rel_path,
        interactive=interactive,
        force=force,
    )


def _prompt_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> bool:
    """Interactive menu for adopting an empty directory deletion."""
    print(f"\nFound host empty directory deletion: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt deletion (removes empty directory from source)")
    print("[2] Discard deletion / Restore (restores directory in next deployment)")
    print("[3] Skip folder")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return True
    elif choice == "2":
        return True
    else:
        return False


def _prompt_non_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> bool:
    """Interactive menu for handling a non-empty directory deletion."""
    src_dir = src_dir_to_render / rel_path
    print(f"\n⚠️  [NON-EMPTY] Host deleted directory '{rel_path}', but source directory contains files!")
    contained_items = (
        sorted(
            p.relative_to(src_dir).as_posix() + ("/" if p.is_dir() else "")
            for p in src_dir.rglob("*")
        )
        if src_dir.is_dir()
        else []
    )
    if contained_items:
        print("Existing contents in source directory:")
        for item in contained_items[:10]:
            print(f"  - {item}")
        if len(contained_items) > 10:
            print(f"  ... and {len(contained_items) - 10} more item(s)")

    print("Reconciliation options:")
    print("[1] Discard deletion / Restore (preserves source files and restores directory on next deployment)")
    print("[2] Adopt deletion (⚠️  DANGER: permanently deletes entire directory tree and ALL files from source!)")
    print("[3] Skip folder")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        return True
    elif choice == "2":
        confirm = input(
            f"⚠️  DANGER: Are you sure you want to permanently delete source directory '{rel_path}' and all {len(contained_items)} item(s)? [y/N]: "
        ).strip().lower()
        if confirm in ("y", "yes"):
            adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
            return True
        print("Aborted deletion adoption.")
        return False
    else:
        return False


def _prompt_file_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> bool:
    """Interactive menu for adopting a file deletion."""
    print(f"\nFound host file deletion: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt deletion (deletes source file/template)")
    print("[2] Discard deletion / Restore (restores file in next deployment)")
    print("[3] Skip file")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return True
    elif choice == "2":
        return True
    else:
        return False


def _handle_single_directory_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> bool:
    """Handles drift reconciliation for a directory deletion."""
    is_empty = not any((src_dir_to_render / rel_path).iterdir())
    assert_source_file_clean(src_dir_to_render / rel_path, pkg=pkg, force=force)
    if not interactive:
        if is_empty:
            adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
            return True
        else:
            logger.warning(
                f"⚠️  [DISCARD] Cannot adopt directory deletion '{rel_path}' because the source directory is not empty. "
                f"Discarding deletion (will restore directory on next deploy)."
            )
            return True
    else:
        if is_empty:
            return _prompt_empty_directory_deletion_interactive(render_engines, pkg, src_dir_to_render, rel_path)
        else:
            return _prompt_non_empty_directory_deletion_interactive(render_engines, pkg, src_dir_to_render, rel_path)


def _handle_single_file_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> bool:
    """Handles drift reconciliation for a file deletion."""
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if target_existing_src is None:
        if not interactive:
            logger.warning(f"⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping.")
        else:
            print(f"\n⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping.")
        return True

    assert_source_file_clean(target_existing_src, pkg=pkg, force=force)
    if not interactive:
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return True
    return _prompt_file_deletion_interactive(render_engines, pkg, src_dir_to_render, rel_path)


def handle_single_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> bool:
    """Handles drift reconciliation for a single file or directory deletion."""
    target_src = src_dir_to_render / rel_path
    if target_src.is_dir() and not target_src.is_symlink():
        return _handle_single_directory_deletion(
            render_engines=render_engines,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        )
    return _handle_single_file_deletion(
        render_engines=render_engines,
        pkg=pkg,
        src_dir_to_render=src_dir_to_render,
        rel_path=rel_path,
        interactive=interactive,
        force=force,
    )


def handle_rename_non_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: str,
    has_patch_conflict: bool,
    accept_conflicts: bool,
    install_file: Optional[Path] = None
) -> bool:
    """Processes a rename drift non-interactively."""
    if patch_content and patch_content.strip():
        logger.debug(f"Diff for rename '{old_rel_path}' -> '{new_rel_path}':\n{patch_content.strip()}")
    new_src_file = adopt_rename(
        render_engines,
        src_dir_to_render,
        old_rel_path,
        new_rel_path,
        patch_content,
        has_patch_conflict=has_patch_conflict,
        install_file=install_file,
        accept_conflicts=accept_conflicts,
    )
    return new_src_file is not None


def handle_rename_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    old_src_file: Path,
    patch_content: str,
    has_patch_conflict: bool
) -> bool:
    """Processes a rename drift interactively."""
    print(f"\nFound host file rename: {old_rel_path} -> {new_rel_path}")
    if patch_content and patch_content.strip():
        print(f"{patch_content.strip()}\n")
    install_file = install_pkg_dir / new_rel_path
    if not has_patch_conflict:
        print("Reconciliation options (Patch applies cleanly):")
        print("[1] Adopt rename (renames template file and applies patch)")
        print("[2] Adopt rename and Edit in Editor (renames, applies patch, then opens template in $EDITOR)")
        print("[3] Open Side-by-Side Reference (renames template, opens template and live file side-by-side)")
        print("[4] Discard rename / Restore (restores file rename in next deployment)")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice in ["1", "2", "3"]:
            new_src_file = adopt_rename(
                render_engines,
                src_dir_to_render,
                old_rel_path,
                new_rel_path,
                patch_content,
                has_patch_conflict=False,
                install_file=install_file,
                accept_conflicts=False,
            )
            if not new_src_file:
                return False
            if choice == "2":
                try:
                    launch_single_file_editor(new_src_file)
                except RuntimeError as e:
                    logger.warning(f"⚠️  Failed to open editor: {e}.")
                    return False
            elif choice == "3":
                return fallback_side_by_side(new_src_file, install_file)
            return True
        elif choice == "4":
            return True
        else:
            return False
    else:
        print(f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto renamed template file '{old_src_file.name}'!")
        print("Choose fallback resolution strategy:")
        print("[1] Over-render & Freeze (overwrites template, original saved to .bak)")
        print("[2] Open Merge Conflict Editor (writes conflict markers and opens editor)")
        print("[3] Open Side-by-Side Reference (opens template and static drift side-by-side)")
        print("[4] Discard rename / Restore")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice in ["1", "2", "3"]:
            # note the patch_content is empty here because we only do rename in this function.
            new_src_file = adopt_rename(
                render_engines,
                src_dir_to_render,
                old_rel_path,
                new_rel_path,
                patch_content="",
                has_patch_conflict=False,
                install_file=install_file,
                accept_conflicts=False,
            )
            if not new_src_file:
                return False

            if choice == "1":
                fallback_over_render(new_src_file, install_file)
                return True
            elif choice == "2":
                # Adjust patch to new file name
                adjusted_patch = generate_adjusted_patch(
                    install_pkg_dir.parent,
                    pkg,
                    new_rel_path,
                    old_rel_path=old_rel_path,
                    target_src_filename=new_src_file.name
                )
                return patch_and_edit(
                    new_src_file,
                    adjusted_patch,
                    install_file=install_file,
                    accept_conflicts=True,
                    open_editor=True,
                )
            elif choice == "3":
                return fallback_side_by_side(new_src_file, install_file)

        if choice == "4":
            return True
        else:
            return False


def handle_single_rename(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    install_base: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    interactive: bool,
    accept_conflicts: bool,
    force: bool = False,
    precomputed_patch: Optional[str] = None,
) -> bool:
    """Handles drift reconciliation for a single file/template rename."""
    # Check if the target already exists in source
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, new_rel_path)
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

    old_src_file = resolve_source_file_path(render_engines, src_dir_to_render, old_rel_path)
    has_patch_conflict = False
    if old_src_file and old_src_file.exists():
        assert_source_file_clean(old_src_file, force=force, pkg=pkg)
        if precomputed_patch is not None:
            patch_content = precomputed_patch
        else:
            patch_content = generate_adjusted_patch(
                install_base,
                pkg,
                new_rel_path,
                old_rel_path=old_rel_path,
                target_src_filename=old_src_file.name
            )
        has_patch_conflict = check_patch_conflicts(old_src_file, patch_content)
    else:
        logger.warning(f"⚠️  Old source file for '{old_rel_path}' not found in package '{pkg}'. Treating as new file addition.")
        return handle_single_addition(
            render_engines, src_pkg_dir, src_dir_to_render, install_pkg_dir, new_rel_path, interactive, force=force
        )

    if not interactive:
        return handle_rename_non_interactive(
            render_engines, src_dir_to_render, old_rel_path, new_rel_path,
            patch_content, has_patch_conflict, accept_conflicts,
            install_file=(install_pkg_dir / new_rel_path)
        )
    else:
        return handle_rename_interactive(
            render_engines, pkg, src_dir_to_render, install_pkg_dir, old_rel_path, new_rel_path,
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
    if patch_content and patch_content.strip():
        logger.debug(f"Diff for '{src_file.name}':\n{patch_content.strip()}")
    if not has_conflict:
        if is_templated:
            return patch_and_edit(
                src_file,
                patch_content,
                install_file=install_file,
                accept_conflicts=False,
                open_editor=False,
            )
        else:
            copy_file(install_file, src_file)
            _sync_file_mode(src_file, install_file)
            return True
    else:
        if accept_conflicts:
            logger.warning(f"⚠️  Applying conflicting patch into file: '{src_file.name}'")
            return patch_and_edit(
                src_file,
                patch_content,
                install_file=install_file,
                accept_conflicts=True,
                open_editor=False,
            )
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
            if patch_content and patch_content.strip():
                print(f"{patch_content.strip()}\n")
            print("Reconciliation options:")
            print("[1] Adopt modifications (applies patch cleanly to template)")
            print("[2] Adopt and Edit in Editor (applies patch, then opens template in $EDITOR)")
            print("[3] Open Side-by-Side Reference (opens template and live file side-by-side for manual merge)")
            print("[4] Discard modifications / Restore (restores file in next deployment)")
            print("[5] Skip file")
            choice = input("Select option [1-5]: ").strip()
            if choice in ["1", "2"]:
                return patch_and_edit(
                    src_file,
                    patch_content,
                    install_file=install_file,
                    accept_conflicts=False,
                    open_editor=(choice == "2"),
                )
            elif choice == "3":
                return fallback_side_by_side(src_file, install_file)
            elif choice == "4":
                return True
            else:
                return False
        else:
            print(f"\nFound modified static config file (Patch applies cleanly): {rel_path}")
            if patch_content and patch_content.strip():
                print(f"{patch_content.strip()}\n")
            print("Reconciliation options:")
            print("[1] Adopt modifications (overwrites source file)")
            print("[2] Adopt and Edit in Editor (overwrites source file, then opens in $EDITOR)")
            print("[3] Open Side-by-Side Reference (opens source file and live file side-by-side for manual merge)")
            print("[4] Discard modifications / Restore (restores file in next deployment)")
            print("[5] Skip file")
            choice = input("Select option [1-5]: ").strip()
            if choice in ["1", "2"]:
                copy_file(install_file, src_file)
                _sync_file_mode(src_file, install_file)
                if choice == "2":
                    try:
                        launch_single_file_editor(src_file)
                    except RuntimeError as e:
                        logger.warning(f"⚠️  Failed to open editor: {e}.")
                        return False
                return True
            elif choice == "3":
                return fallback_side_by_side(src_file, install_file)
            elif choice == "4":
                return True
            else:
                return False
    else:
        print(f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto file '{src_file.name}'!")
        if patch_content and patch_content.strip():
            print(f"{patch_content.strip()}\n")
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
            return patch_and_edit(
                src_file,
                patch_content,
                install_file=install_file,
                accept_conflicts=True,
                open_editor=True,
            )
        elif choice == "3":
            return fallback_side_by_side(src_file, install_file)
        elif choice == "4":
            return True
        else:
            return False


def handle_single_modification(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    install_base: Path,
    rel_path: Path,
    interactive: bool,
    accept_conflicts: bool,
    force: bool = False,
    precomputed_patch: Optional[str] = None,
) -> bool:
    """Handles drift reconciliation for a single file/template modification."""
    src_file = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    pkg_rel_path = Path(pkg) / rel_path
    install_file = install_pkg_dir / rel_path

    if not src_file:
        # Symmetrically handle static file as an addition so the user has full choice in interactive mode.
        return handle_single_addition(
            render_engines, src_pkg_dir, src_dir_to_render,
            install_pkg_dir, rel_path, interactive, force=force
        )

    assert_source_file_clean(src_file, force=force, pkg=pkg)

    is_templated = ".envst" in src_file.name or ".mustache" in src_file.name
    if precomputed_patch is not None:
        patch_content = precomputed_patch
    else:
        patch_content = generate_unified_patch(install_base, pkg_rel_path)

    # Check if there are content diff hunks in the patch
    has_content_hunks = any(line.startswith("@@") for line in patch_content.splitlines())

    if not is_templated:
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


# =====================================================================
# Layer 4: Single-Package Drift Adoption
# =====================================================================

def execute_package_adopt(
    workspace_config: WorkspaceConfig,
    plan: PackageAdoptPlan,
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> PackageAdoptResult:
    """Executes adoption for a single PackageAdoptPlan, reconciling source files and staging adopted paths.

    Note:
        This function stages resolved/adopted files ('git add -- <resolved_paths>') in the install/
        repository index, but does NOT commit them. Committing staged changes is performed at
        Layer 5 orchestration (e.g. execute_adopt_repo) or via commit_staged_repo_changes.
    """
    if not plan.has_drifts:
        logger.info(f"✨ Package '{plan.package}' has no drifts.")
        return PackageAdoptResult(package=plan.package, status="SUCCESS")

    # Trigger pre_source hook before adopting drifts into source directory
    trigger_pre_source_hook(workspace_config, plan.package, flags=flags)

    adopted_additions: List[str] = []
    adopted_deletions: List[str] = []
    adopted_renames: List[str] = []
    adopted_modifications: List[str] = []
    skipped_files: List[str] = []
    resolved_paths: List[Path] = []

    # 1. Process Additions
    for rel_path in plan.additions:
        resolved = handle_single_addition(
            plan.render_engines, plan.src_pkg_dir, plan.src_dir_to_render, plan.install_pkg_dir,
            rel_path, interactive, force=force
        )
        if resolved:
            adopted_additions.append(str(rel_path))
            resolved_paths.append(rel_path)
        else:
            skipped_files.append(str(rel_path))

    # 2. Process Deletions
    for rel_path in plan.deletions:
        resolved = handle_single_deletion(
            plan.render_engines, plan.package, plan.src_dir_to_render, rel_path, interactive, force=force
        )
        if resolved:
            adopted_deletions.append(str(rel_path))
            resolved_paths.append(rel_path)
        else:
            skipped_files.append(str(rel_path))

    # 3. Process Renames
    for old_rel_path, new_rel_path in plan.renames:
        patch_content = plan.patches.get(f"{old_rel_path}->{new_rel_path}")
        resolved = handle_single_rename(
            plan.render_engines, plan.package, plan.src_pkg_dir, plan.src_dir_to_render,
            plan.install_pkg_dir, workspace_config.install_path,
            old_rel_path, new_rel_path, interactive, accept_conflicts, force=force,
            precomputed_patch=patch_content,
        )
        if resolved:
            adopted_renames.append(f"{old_rel_path} -> {new_rel_path}")
            resolved_paths.append(old_rel_path)
            resolved_paths.append(new_rel_path)
        else:
            skipped_files.append(str(old_rel_path))
            skipped_files.append(str(new_rel_path))

    # 4. Process Modifications
    for rel_path in plan.modifications:
        patch_content = plan.patches.get(str(rel_path))
        resolved = handle_single_modification(
            plan.render_engines, plan.package, plan.src_pkg_dir, plan.src_dir_to_render,
            plan.install_pkg_dir, workspace_config.install_path,
            rel_path, interactive, accept_conflicts, force=force,
            precomputed_patch=patch_content,
        )
        if resolved:
            adopted_modifications.append(str(rel_path))
            resolved_paths.append(rel_path)
        else:
            skipped_files.append(str(rel_path))

    # 5. Staging ONLY resolved files in install/ repository
    if resolved_paths:
        stage_args = [(Path(plan.package) / p).as_posix() for p in resolved_paths]
        res_add = subprocess.run(
            ["git", "-C", str(workspace_config.install_path), "add", "--", *stage_args],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_add.returncode != 0:
            logger.warning(
                f"Staging adopted changes for '{plan.package}' exited with code {res_add.returncode}: {res_add.stderr.strip()}"
            )

    return PackageAdoptResult(
        package=plan.package,
        adopted_additions=adopted_additions,
        adopted_modifications=adopted_modifications,
        adopted_deletions=adopted_deletions,
        adopted_renames=adopted_renames,
        skipped_files=skipped_files,
        status="FAILED" if skipped_files else "SUCCESS",
    )


def adopt_one_package_drifts(
    workspace_config: WorkspaceConfig,
    pkg: str,
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> PackageAdoptResult:
    """Adopts drifts for a single package using decoupled plan and execution phases.

    Note:
        adopt_one_package_drifts does NOT commit the install/ repo; it only reconciles source files
        and stages adopted paths in the install/ repository index. Committing staged changes is
        the responsibility of Layer 5 orchestration (execute_adopt_repo / run_primitive_adopt_drifts)
        or explicit commit helpers (commit_staged_repo_changes).
    """
    plan = get_package_drifts(workspace_config, pkg)
    if dry_run:
        if plan.has_drifts:
            dry_run_adopt(plan)
            return PackageAdoptResult(
                package=pkg,
                adopted_additions=[str(p) for p in plan.additions],
                adopted_deletions=[str(p) for p in plan.deletions],
                adopted_modifications=[str(p) for p in plan.modifications],
                adopted_renames=[f"{old} -> {new}" for old, new in plan.renames],
                status="SUCCESS",
            )
        else:
            logger.info(f"✨ Package '{pkg}' has no drifts.")
            return PackageAdoptResult(package=pkg, status="SUCCESS")

    return execute_package_adopt(
        workspace_config=workspace_config,
        plan=plan,
        interactive=interactive,
        accept_conflicts=accept_conflicts,
        force=force,
        flags=flags,
    )


# =====================================================================
# Layer 5: Public Primitive Entry Point
# =====================================================================

def plan_adopt_repo(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
) -> AdoptPlan:
    """Discovers drifted packages and creates an AdoptPlan containing PackageAdoptPlans."""
    if not package_names:
        package_names = get_drifted_packages(workspace_config)

    plans = [
        get_package_drifts(workspace_config, pkg)
        for pkg in package_names
    ]
    return AdoptPlan(plans=plans)


def execute_adopt_repo(
    workspace_config: WorkspaceConfig,
    plan: AdoptPlan,
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> AdoptResult:
    """Executes adoption for all plans in AdoptPlan, staging adopted files and committing changes."""
    package_results: List[PackageAdoptResult] = []
    has_any_adopted = False

    for pkg_plan in plan.plans:
        pkg_res = execute_package_adopt(
            workspace_config=workspace_config,
            plan=pkg_plan,
            interactive=interactive,
            accept_conflicts=accept_conflicts,
            force=force,
            flags=flags,
        )
        package_results.append(pkg_res)
        if pkg_res.adopted_additions or pkg_res.adopted_deletions or pkg_res.adopted_modifications or pkg_res.adopted_renames:
            has_any_adopted = True

    # Commit staged adopted changes in install base
    if has_any_adopted:
        adopted_pkgs = [
            p.package for p in package_results
            if p.adopted_additions or p.adopted_deletions or p.adopted_modifications or p.adopted_renames
        ]
        pkg_word = "package" if len(adopted_pkgs) == 1 else "packages"
        commit_staged_repo_changes(
            repo_path=workspace_config.install_path,
            commit_message=f"Adopt: Resolved and locked drifts for {pkg_word} {', '.join(adopted_pkgs)}",
            repo_name="install repo"
        )

    all_success = all(p.status == "SUCCESS" for p in package_results) if package_results else True
    return AdoptResult(
        command="adopt",
        status="SUCCESS" if all_success else "FAILED",
        packages=package_results,
        dry_run=False,
    )


def run_primitive_adopt_drifts(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> AdoptResult:
    """High-level orchestrator for adopting system drifts back to declarative templates (Primitive Adopt).

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Specific package name(s) to adopt, or empty/omitted to discover all drifted packages.
        interactive: If True, interactively prompts for conflict resolution.
        accept_conflicts: If True, writes Git conflict merge markers directly into source template files.
        force: If True, bypasses the Git cleanliness safeguard on target source files,
            allowing adoption to proceed even if uncommitted modifications exist in those files.
        dry_run: If True, previews drift adoption without writing patches to disk.
        flags: Optional HookExecFlags controlling hook execution options.

    Returns:
        AdoptResult containing detailed results for all adopted packages.
    """
    # 1. Planning
    plan = plan_adopt_repo(workspace_config, package_names=package_names)
    if not plan.plans or not plan.has_drifts:
        if not package_names:
            logger.info("✨ No drifted packages found in local state database.")
        else:
            for p in plan.plans:
                logger.info(f"✨ Package '{p.package}' has no drifts.")
        return AdoptResult(
            command="adopt",
            status="SUCCESS",
            packages=[
                PackageAdoptResult(package=p.package, status="SUCCESS")
                for p in plan.plans
            ],
            dry_run=dry_run,
        )

    # 2. Dry-run Mode
    if dry_run:
        package_results: List[PackageAdoptResult] = []
        for pkg_plan in plan.plans:
            if pkg_plan.has_drifts:
                dry_run_adopt(pkg_plan)
                package_results.append(PackageAdoptResult(
                    package=pkg_plan.package,
                    adopted_additions=[str(p) for p in pkg_plan.additions],
                    adopted_deletions=[str(p) for p in pkg_plan.deletions],
                    adopted_modifications=[str(p) for p in pkg_plan.modifications],
                    adopted_renames=[f"{old} -> {new}" for old, new in pkg_plan.renames],
                    status="SUCCESS",
                ))
            else:
                logger.info(f"✨ Package '{pkg_plan.package}' has no drifts.")
                package_results.append(PackageAdoptResult(package=pkg_plan.package, status="SUCCESS"))

        return AdoptResult(
            command="adopt",
            status="SUCCESS",
            packages=package_results,
            dry_run=True,
        )

    # 3. Execution
    return execute_adopt_repo(
        workspace_config=workspace_config,
        plan=plan,
        interactive=interactive,
        accept_conflicts=accept_conflicts,
        force=force,
        flags=flags,
    )
