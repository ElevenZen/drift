"""Single-File Reconciliation Actions for Drift Adoption (Layer 2).

Architecture & Call Chain Overview:
===================================
Provides atomic filesystem and editor operations for adopting or ignoring individual
drifted items into `src/<pkg>/`:

1. Mode & File Operations:
   - `_sync_file_mode`: Copies `st_mode` permissions from `install_file` onto `src_file`.
   - `adopt_addition`: Copies a newly added host file or creates an empty directory in `src/`.
   - `ignore_addition`: Removes an untracked addition from `install/`, unstages it via
     `git rm --cached`, and appends its pattern to `.drift_ignore`.
   - `adopt_deletion`: Deletes a removed file, template, or directory tree from `src/`.
2. Patch & Rename Operations:
   - `patch_and_edit`: Applies a unified/de-rendered patch via `apply_source_patch`,
     synchronizes permissions, and optionally launches `$EDITOR`.
   - `adopt_rename`: Renames the source template using `RenderEngineRegistry.make_new_template_name`
     and applies any content/mode diff via `patch_and_edit`.
3. Interactive Fallback Strategies:
   - `fallback_over_render`: Backs up the template to `.bak` and overwrites it with static host content.
   - `fallback_side_by_side`: Launches a side-by-side comparison editor (`launch_side_by_side_editor`).
"""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from typing import Optional

from ..config.render_engine_config import RenderEngineRegistry
from ..core.constants import DRIFT_IGNORE_FILE_NAME, DRIFT_KEEP_FILE_NAME
from ..models.adopt_models import AdoptPatch
from ..utils.editor_utils import launch_side_by_side_editor, launch_single_file_editor
from ..utils.file_ops import copy_file, ensure_dir, remove_file_or_empty_dir, remove_tree
from ..utils.func_utils import choose, when_none, when_not_none
from .adopt_patch import apply_source_patch
from .adopt_plan import resolve_source_file_path

logger = logging.getLogger(__name__)


def _sync_file_mode(target_file: Path, patch_from: Path) -> None:
    """Synchronizes file mode/permissions from patch_from onto target_file."""
    try:
        target_file.chmod(patch_from.stat().st_mode)
    except Exception as e:
        logger.warning(
            f"Failed to synchronize file permissions from '{patch_from}' to '{target_file}': {e}"
        )


def ensure_empty_keep_file(keep_file: Path) -> None:
    """Ensures a .drift_keep stub file in install/ is a 0-byte regular file with standard permissions."""
    if keep_file.is_symlink():
        keep_file.unlink()
        keep_file.write_bytes(b"")
    elif keep_file.is_file():
        if keep_file.stat().st_size > 0:
            keep_file.write_bytes(b"")
        try:
            keep_file.chmod(0o644)
        except OSError:
            pass


def adopt_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Copies a wild host-side added file or creates an empty directory in the declarative source folder."""
    if rel_path.name == DRIFT_KEEP_FILE_NAME:
        ensure_dir(pkg_dir / rel_path.parent)
        ensure_empty_keep_file(install_pkg_dir / rel_path)
        return
    dest = pkg_dir / rel_path
    src_install = install_pkg_dir / rel_path
    if src_install.is_dir():
        ensure_dir(dest)
        ensure_empty_keep_file(src_install / DRIFT_KEEP_FILE_NAME)
    else:
        copy_file(src_install, dest)


def ignore_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Unlinks the file/directory from install base and registers pattern in .drift_ignore."""
    install_item = install_pkg_dir / rel_path
    was_dir = install_item.is_dir()
    if install_item.is_dir() and not install_item.is_symlink():
        remove_tree(install_item)
    elif install_item.exists() or install_item.is_symlink():
        remove_file_or_empty_dir(install_item)

    install_base = install_pkg_dir.parent
    rel_install_base = Path(install_pkg_dir.name) / rel_path
    # check=False is intentional: file may not be tracked in the git index yet.
    res_rm = subprocess.run(
        [
            "git",
            "-C",
            install_base.as_posix(),
            "rm",
            "-r",
            "--cached",
            "-f",
            "--",
            rel_install_base.as_posix(),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_rm.returncode != 0:
        logger.debug(
            f"git rm --cached exited with code {res_rm.returncode} for '{rel_install_base}': {res_rm.stderr.strip()}"
        )

    ignore_file = pkg_dir / DRIFT_IGNORE_FILE_NAME
    pattern = choose(was_dir, f"{rel_path.as_posix()}/", rel_path.as_posix())
    with ignore_file.open("a", encoding="utf-8") as f:
        f.write(f"\n{pattern}\n")


def adopt_deletion(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
) -> None:
    """Symmetrically deletes the corresponding file or directory from declarative source folder."""
    src_dir = src_dir_to_render / rel_path
    if src_dir.is_dir() and not src_dir.is_symlink():
        remove_tree(src_dir)
        return
    src_match = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if src_match and (src_match.path.exists() or src_match.path.is_symlink()):
        remove_file_or_empty_dir(src_match.path)


def patch_and_edit(
    target_file: Path,
    patch_content: AdoptPatch,
    accept_conflicts: bool = False,
    open_editor: bool = False,
) -> bool:
    """Applies an AdoptPatch (clean or with merge markers) to target_file, synchronizes file permissions from patch_content.patch_from, and optionally opens $EDITOR.

    Note: apply_source_patch is executed only when '@@' diff hunks or conflict clusters exist.
    File mode/permissions are synchronized separately via _sync_file_mode when patch_content.patch_from exists.
    """
    if patch_content.has_changes:
        success = apply_source_patch(target_file, patch_content, accept_conflicts=accept_conflicts)
        if not success:
            logger.error(
                f"Failed to apply patch to template file {target_file.name}. Please check manually."
            )
            return False
    if patch_content.patch_from != Path() and patch_content.patch_from.exists():
        _sync_file_mode(target_file, patch_content.patch_from)
    if open_editor:
        try:
            launch_single_file_editor(target_file)
        except RuntimeError as e:
            logger.warning(f"⚠️  Failed to open editor: {e}. Skipping file adoption.")
            return False
    return True


def adopt_rename(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: AdoptPatch,
    accept_conflicts: bool = False,
) -> Optional[Path]:
    """Symmetrically renames the source file/template and applies any content patch."""
    old_src_match = when_none(
        patch_content.src_match,
        lambda: resolve_source_file_path(render_engines, src_dir_to_render, old_rel_path),
    )
    if old_src_match is None or not old_src_match.path.exists():
        logger.error(
            f"❌ [CONFLICT] Source file for '{old_rel_path}' not found in source directory. Skipping rename."
        )
        return None

    old_src_file = old_src_match.path
    file_kind = choose(patch_content.is_templated, "template file", "file")

    if patch_content.has_conflict:
        if accept_conflicts:
            logger.warning(
                f"⚠️  Applying conflicting patch into renamed {file_kind}: '{old_src_file.name}'"
            )
        else:
            logger.error(
                f"❌ [CONFLICT] Cannot apply system diff cleanly onto renamed {file_kind} '{old_src_file.name}'. Skipping."
            )
            logger.error(
                "   Run 'drift adopt --interactive' or pass '--accept-conflicts' to resolve."
            )
            return None

    new_src_name = render_engines.make_new_template_name(old_src_file.name, new_rel_path.name)
    new_src_file = src_dir_to_render / new_rel_path.parent / new_src_name
    new_src_file.parent.mkdir(parents=True, exist_ok=True)
    copy_file(old_src_file, new_src_file)

    success = patch_and_edit(
        new_src_file,
        patch_content,
        accept_conflicts=accept_conflicts,
        open_editor=False,
    )
    if not success:
        new_src_file.unlink(missing_ok=True)
        return None

    if old_src_file != new_src_file:
        old_src_file.unlink(missing_ok=True)

    return new_src_file


def fallback_over_render(src_file: Path, static_file: Path) -> None:
    """Backs up the original template to .bak and overwrites it with static file content (freezing template)."""
    bak_file = src_file.with_suffix(src_file.suffix + ".bak")
    copy_file(src_file, bak_file)
    copy_file(static_file, src_file)
    logger.warning(
        f"⚠️  [FREEZE] Overwrote template '{src_file.name}' with static content. Original template backed up to '{bak_file.name}'."
    )


def fallback_side_by_side(src_file: Path, install_file: Path) -> bool:
    """Launches editor to display both template and the final compiled/static drift as side-by-side reference."""
    try:
        launch_side_by_side_editor([(src_file, install_file)])
        return True
    except RuntimeError as e:
        logger.warning(f"⚠️  Failed to open side-by-side editor: {e}. Skipping file adoption.")
        return False
