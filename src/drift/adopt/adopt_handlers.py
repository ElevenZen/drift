"""Interactive and Non-Interactive Single-Item Drift Dispatchers (Layer 3).

Architecture & Call Chain Overview:
===================================
Routes individual additions, deletions, renames, and modifications through either
automated reconciliation or interactive (`-i`) terminal menus:

1. Addition Dispatchers:
   - `_prompt_addition_collision_interactive`, `_prompt_directory_addition_interactive`,
     `_prompt_file_addition_interactive`
   - `_handle_single_directory_addition`, `_handle_single_file_addition`, `handle_single_addition`
2. Deletion Dispatchers:
   - `_prompt_empty_directory_deletion_interactive`, `_prompt_non_empty_directory_deletion_interactive`,
     `_prompt_file_deletion_interactive`
   - `_handle_single_directory_deletion`, `_handle_single_file_deletion`, `handle_single_deletion`
3. Rename Dispatchers:
   - `handle_rename_non_interactive`, `handle_rename_interactive`, `handle_single_rename`
4. Modification Dispatchers:
   - `handle_modification_non_interactive`, `handle_modification_interactive`, `handle_single_modification`
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..config.render_engine_config import RenderEngineRegistry, RenderSourceMatch
from ..core.constants import DRIFT_KEEP_FILE_NAME
from ..models.adopt_models import ItemAdoptOutcome
from ..utils.file_ops import copy_file
from ..utils.func_utils import branch, choose, when_none, when_not_none
from ..utils.path_utils import to_relative_posix
from . import adopt_actions
from .adopt_actions import (
    _sync_file_mode,
    adopt_addition,
    adopt_deletion,
    adopt_rename,
    ensure_empty_keep_file,
    fallback_over_render,
    fallback_side_by_side,
    ignore_addition,
    patch_and_edit,
)
from .adopt_patch import check_patch_conflicts, check_patch_has_hunks
from .adopt_plan import (
    _precompute_modification_patch,
    _precompute_rename_patch,
    _translate_keep_to_dir,
    assert_source_file_clean,
    resolve_source_file_path,
)

logger = logging.getLogger(__name__)


def _prompt_addition_collision_interactive(
    rel_path: Path, incoming_is_dir: bool
) -> ItemAdoptOutcome:
    """Prompts the user when an addition collides with an existing file/directory in source."""
    incoming_type = choose(incoming_is_dir, "directory", "file")
    existing_type = choose(incoming_is_dir, "file", "directory")
    print(
        f"\n⚠️  [CONFLICT] Cannot adopt {incoming_type} '{rel_path}' because a {existing_type} with the same name exists in source!"
    )
    print("Reconciliation options:")
    print(f"[1] Discard addition / Restore (removes {incoming_type} on host next deployment)")
    print(f"[2] Skip {incoming_type}")
    choice = input("Select option [1-2]: ").strip()
    return choose(choice == "1", ItemAdoptOutcome.ADOPTED, ItemAdoptOutcome.SKIPPED)


def _prompt_directory_addition_interactive(
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
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
        return ItemAdoptOutcome.ADOPTED
    if choice == "2":
        ignore_addition(src_pkg_dir, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    if choice == "3":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _prompt_file_addition_interactive(
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
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
        return ItemAdoptOutcome.ADOPTED
    if choice == "2":
        ignore_addition(src_pkg_dir, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    if choice == "3":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _handle_single_directory_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles adoption of an empty directory addition."""
    target_rel = _translate_keep_to_dir(rel_path)
    keep_file = branch(
        rel_path.name == DRIFT_KEEP_FILE_NAME,
        lambda: install_pkg_dir / rel_path,
        lambda: install_pkg_dir / target_rel / DRIFT_KEEP_FILE_NAME,
    )
    if (src_dir_to_render / target_rel).is_dir():
        ensure_empty_keep_file(keep_file)
        logger.info(f"Directory '{target_rel}' already exists in source. Skipping addition.")
        return ItemAdoptOutcome.ADOPTED

    if (
        src_dir_to_render / target_rel
    ).is_file() or resolve_source_file_path(render_engines, src_dir_to_render, target_rel) is not None:
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Cannot adopt directory '{target_rel}' because a file already exists at that path in source. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        outcome = _prompt_addition_collision_interactive(target_rel, incoming_is_dir=True)
        if outcome == ItemAdoptOutcome.ADOPTED:
            ensure_empty_keep_file(keep_file)
        return outcome

    assert_source_file_clean(
        src_dir_to_render / target_rel, force=force, pkg=install_pkg_dir.name
    )
    if not interactive:
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    outcome = _prompt_directory_addition_interactive(
        src_pkg_dir, src_dir_to_render, install_pkg_dir, target_rel
    )
    if outcome == ItemAdoptOutcome.ADOPTED:
        ensure_empty_keep_file(keep_file)
    return outcome


def _handle_single_file_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles adoption of a regular file addition."""
    if (src_dir_to_render / rel_path).is_dir():
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Cannot adopt file '{rel_path}' because a directory already exists at that path in source. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=False)

    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if target_existing_src is not None:
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Cannot adopt addition '{rel_path}' because the target already exists in source. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=False)

    target_src_file = src_dir_to_render / rel_path
    assert_source_file_clean(target_src_file, force=force, pkg=install_pkg_dir.name)
    if not interactive:
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    return _prompt_file_addition_interactive(
        src_pkg_dir, src_dir_to_render, install_pkg_dir, rel_path
    )


def handle_single_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file or directory addition."""
    return branch(
        (install_pkg_dir / rel_path).is_dir() or rel_path.name == DRIFT_KEEP_FILE_NAME,
        lambda: _handle_single_directory_addition(
            render_engines=render_engines,
            src_pkg_dir=src_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            install_pkg_dir=install_pkg_dir,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        ),
        lambda: _handle_single_file_addition(
            render_engines=render_engines,
            src_pkg_dir=src_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            install_pkg_dir=install_pkg_dir,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        ),
    )


def _prompt_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for adopting an empty directory deletion."""
    print(f"\nFound host empty directory deletion: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt deletion (removes empty directory from source)")
    print("[2] Discard deletion / Restore (restores directory in next deployment)")
    print("[3] Skip folder")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return ItemAdoptOutcome.ADOPTED
    if choice == "2":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _prompt_non_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for handling a non-empty directory deletion."""
    src_dir = src_dir_to_render / rel_path
    print(
        f"\n⚠️  [NON-EMPTY] Host deleted directory '{rel_path}', but source directory contains files!"
    )
    contained_items = branch(
        src_dir.is_dir(),
        lambda: sorted(
            to_relative_posix(p, src_dir) + choose(p.is_dir(), "/", "")
            for p in src_dir.rglob("*")
        ),
        lambda: [],
    )
    if contained_items:
        print("Existing contents in source directory:")
        for item in contained_items[:10]:
            print(f"  - {item}")
        if len(contained_items) > 10:
            print(f"  ... and {len(contained_items) - 10} more item(s)")

    print("Reconciliation options:")
    print(
        "[1] Discard deletion / Restore (preserves source files and restores directory on next deployment)"
    )
    print(
        "[2] Adopt deletion (⚠️  DANGER: permanently deletes entire directory tree and ALL files from source!)"
    )
    print("[3] Skip folder")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        return ItemAdoptOutcome.ADOPTED
    if choice == "2":
        confirm = (
            input(
                f"⚠️  DANGER: Are you sure you want to permanently delete source directory '{rel_path}' and all {len(contained_items)} item(s)? [y/N]: "
            )
            .strip()
            .lower()
        )
        if confirm in ("y", "yes"):
            adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
            return ItemAdoptOutcome.ADOPTED
        print("Aborted deletion adoption.")
        return ItemAdoptOutcome.SKIPPED
    return ItemAdoptOutcome.SKIPPED


def _prompt_file_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for adopting a file deletion."""
    print(f"\nFound host file deletion: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt deletion (deletes source file/template)")
    print("[2] Discard deletion / Restore (restores file in next deployment)")
    print("[3] Skip file")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return ItemAdoptOutcome.ADOPTED
    if choice == "2":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _handle_single_directory_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a directory deletion."""
    is_empty = not any((src_dir_to_render / rel_path).iterdir())
    assert_source_file_clean(src_dir_to_render / rel_path, pkg=pkg, force=force)
    if not interactive:
        if is_empty:
            adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
            return ItemAdoptOutcome.ADOPTED
        logger.warning(
            f"⚠️  [DISCARD] Cannot adopt directory deletion '{rel_path}' because the source directory is not empty. "
            f"Discarding deletion (will restore directory on next deploy)."
        )
        return ItemAdoptOutcome.ADOPTED

    return branch(
        is_empty,
        lambda: _prompt_empty_directory_deletion_interactive(
            render_engines, pkg, src_dir_to_render, rel_path
        ),
        lambda: _prompt_non_empty_directory_deletion_interactive(
            render_engines, pkg, src_dir_to_render, rel_path
        ),
    )


def _handle_single_file_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a file deletion."""
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if target_existing_src is None:
        if not interactive:
            logger.warning(
                f"⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping."
            )
        else:
            print(
                f"\n⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping."
            )
        return ItemAdoptOutcome.ADOPTED

    assert_source_file_clean(target_existing_src.path, pkg=pkg, force=force)
    if not interactive:
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return ItemAdoptOutcome.ADOPTED
    return _prompt_file_deletion_interactive(render_engines, pkg, src_dir_to_render, rel_path)


def handle_single_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file or directory deletion."""
    target_src = src_dir_to_render / rel_path
    return branch(
        target_src.is_dir() and not target_src.is_symlink(),
        lambda: _handle_single_directory_deletion(
            render_engines=render_engines,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        ),
        lambda: _handle_single_file_deletion(
            render_engines=render_engines,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        ),
    )


def handle_rename_non_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: str,
    patch_from: Path,
    has_patch_conflict: bool,
    accept_conflicts: bool,
) -> ItemAdoptOutcome:
    """Processes a rename drift non-interactively."""
    if patch_content and patch_content.strip():
        logger.debug(
            f"Diff for rename '{old_rel_path}' -> '{new_rel_path}':\n{patch_content.strip()}"
        )
    new_src_file = adopt_rename(
        render_engines,
        src_dir_to_render,
        old_rel_path,
        new_rel_path,
        patch_content,
        patch_from=patch_from,
        has_patch_conflict=has_patch_conflict,
        accept_conflicts=accept_conflicts,
    )
    return choose(
        new_src_file is not None,
        ItemAdoptOutcome.ADOPTED,
        ItemAdoptOutcome.CONFLICT,
    )


def handle_rename_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    old_src_file: Path,
    patch_content: str,
    has_patch_conflict: bool,
) -> ItemAdoptOutcome:
    """Processes a rename drift interactively."""
    print(f"\nFound host file rename: {old_rel_path} -> {new_rel_path}")
    if patch_content and patch_content.strip():
        print(f"{patch_content.strip()}\n")
    install_file = install_pkg_dir / new_rel_path
    if not has_patch_conflict:
        print("Reconciliation options (Patch applies cleanly):")
        print("[1] Adopt rename (renames template file and applies patch)")
        print(
            "[2] Adopt rename and Edit in Editor (renames, applies patch, then opens template in $EDITOR)"
        )
        print(
            "[3] Open Side-by-Side Reference (renames template, opens template and live file side-by-side)"
        )
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
                patch_from=install_file,
                has_patch_conflict=False,
                accept_conflicts=False,
            )
            if not new_src_file:
                return ItemAdoptOutcome.CONFLICT
            if choice == "2":
                try:
                    adopt_actions.launch_single_file_editor(new_src_file)
                except RuntimeError as e:
                    logger.warning(f"⚠️  Failed to open editor: {e}.")
                    return ItemAdoptOutcome.SKIPPED
            elif choice == "3":
                return choose(
                    fallback_side_by_side(new_src_file, install_file),
                    ItemAdoptOutcome.ADOPTED,
                    ItemAdoptOutcome.SKIPPED,
                )
            return ItemAdoptOutcome.ADOPTED
        if choice == "4":
            return ItemAdoptOutcome.ADOPTED
        return ItemAdoptOutcome.SKIPPED

    print(
        f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto renamed template file '{old_src_file.name}'!"
    )
    print("Choose fallback resolution strategy:")
    print("[1] Write Merge Conflicts (writes conflict markers to file)")
    print("[2] Open Merge Conflict Editor (writes conflict markers and opens editor)")
    print("[3] Open Side-by-Side Reference (opens template and static drift side-by-side)")
    print("[4] Over-render & Freeze (overwrites template, original saved to .bak)")
    print("[5] Discard rename / Restore")
    print("[6] Skip file")
    choice = input("Select option [1-6]: ").strip()
    if choice in ["1", "2", "3", "4"]:
        apply_conflict_patch = choice in ["1", "2"]
        new_src_file = adopt_rename(
            render_engines,
            src_dir_to_render,
            old_rel_path,
            new_rel_path,
            patch_content=choose(apply_conflict_patch, patch_content, ""),
            patch_from=install_file,
            has_patch_conflict=apply_conflict_patch,
            accept_conflicts=apply_conflict_patch,
        )
        if not new_src_file:
            return ItemAdoptOutcome.CONFLICT

        if choice == "1":
            return ItemAdoptOutcome.ADOPTED
        if choice == "2":
            try:
                adopt_actions.launch_single_file_editor(new_src_file)
                return ItemAdoptOutcome.ADOPTED
            except RuntimeError as e:
                logger.warning(f"⚠️  Failed to open editor: {e}. Skipping file adoption.")
                return ItemAdoptOutcome.SKIPPED
        if choice == "3":
            return choose(
                fallback_side_by_side(new_src_file, install_file),
                ItemAdoptOutcome.ADOPTED,
                ItemAdoptOutcome.SKIPPED,
            )
        if choice == "4":
            fallback_over_render(new_src_file, install_file)
            return ItemAdoptOutcome.ADOPTED

    return choose(choice == "5", ItemAdoptOutcome.ADOPTED, ItemAdoptOutcome.SKIPPED)


def handle_single_rename(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    install_base: Path,
    render_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    interactive: bool,
    accept_conflicts: bool,
    force: bool = False,
    precomputed_patch: Optional[str] = None,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file/template rename."""
    old_src_match = resolve_source_file_path(render_engines, src_dir_to_render, old_rel_path)
    target_existing_src = resolve_source_file_path(
        render_engines, src_dir_to_render, new_rel_path
    )

    if old_src_match is not None and target_existing_src is not None:
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Cannot rename '{old_rel_path}' to '{new_rel_path}' because the target already exists in source. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        print(f"\n⚠️  [CONFLICT] Target file '{new_rel_path}' already exists in source!")
        print("Reconciliation options:")
        print("[1] Discard rename / Restore (restores original on host next deployment)")
        print("[2] Skip file")
        choice = input("Select option [1-2]: ").strip()
        return choose(choice == "1", ItemAdoptOutcome.ADOPTED, ItemAdoptOutcome.SKIPPED)

    src_match = when_none(old_src_match, target_existing_src)
    target_src_file: Path = when_none(
        when_not_none(src_match, lambda m: m.path),
        src_dir_to_render / new_rel_path,
    )
    assert_source_file_clean(target_src_file, force=force, pkg=pkg)
    patch_content = branch(
        precomputed_patch is not None,
        lambda: precomputed_patch or "",
        lambda: _precompute_rename_patch(
            install_base=install_base,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            old_rel=old_rel_path,
            new_rel=new_rel_path,
        ),
    )
    has_patch_conflict = branch(
        src_match is not None and target_src_file.exists(),
        lambda: check_patch_conflicts(target_src_file, patch_content),
        lambda: False,
    )

    return branch(
        not interactive,
        lambda: handle_rename_non_interactive(
            render_engines,
            src_dir_to_render,
            old_rel_path,
            new_rel_path,
            patch_content,
            patch_from=(install_pkg_dir / new_rel_path),
            has_patch_conflict=has_patch_conflict,
            accept_conflicts=accept_conflicts,
        ),
        lambda: handle_rename_interactive(
            render_engines,
            src_dir_to_render,
            install_pkg_dir,
            old_rel_path,
            new_rel_path,
            target_src_file,
            patch_content,
            has_patch_conflict,
        ),
    )


def handle_modification_non_interactive(
    src_file: Path,
    install_file: Path,
    patch_content: str,
    is_templated: bool,
    has_conflict: bool,
    accept_conflicts: bool,
) -> ItemAdoptOutcome:
    """Processes a modification drift non-interactively."""
    if patch_content and patch_content.strip():
        logger.debug(f"Diff for '{src_file.name}':\n{patch_content.strip()}")
    if not has_conflict:
        if is_templated or check_patch_has_hunks(patch_content):
            return choose(
                patch_and_edit(
                    src_file,
                    patch_content,
                    patch_from=install_file,
                    accept_conflicts=False,
                    open_editor=False,
                ),
                ItemAdoptOutcome.ADOPTED,
                ItemAdoptOutcome.CONFLICT,
            )
        copy_file(install_file, src_file)
        _sync_file_mode(src_file, install_file)
        return ItemAdoptOutcome.ADOPTED

    if accept_conflicts:
        logger.warning(f"⚠️  Applying conflicting patch into file: '{src_file.name}'")
        return choose(
            patch_and_edit(
                src_file,
                patch_content,
                patch_from=install_file,
                accept_conflicts=True,
                open_editor=False,
            ),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.CONFLICT,
        )

    logger.error(
        f"❌ [CONFLICT] Cannot apply system diff cleanly onto file '{src_file.name}'. Skipping."
    )
    logger.error("   Run 'drift adopt --interactive' or pass '--accept-conflicts' to resolve.")
    return ItemAdoptOutcome.CONFLICT


def handle_modification_interactive(
    src_file: Path,
    install_file: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    patch_content: str,
    is_templated: bool,
    has_conflict: bool,
) -> ItemAdoptOutcome:
    """Processes a modification drift interactively."""
    if not has_conflict:
        if is_templated:
            print(f"\nFound modified templated file (Patch applies cleanly): {rel_path}")
            if patch_content and patch_content.strip():
                print(f"{patch_content.strip()}\n")
            print("Reconciliation options:")
            print("[1] Adopt modifications (applies patch cleanly to template)")
            print(
                "[2] Adopt and Edit in Editor (applies patch, then opens template in $EDITOR)"
            )
            print(
                "[3] Open Side-by-Side Reference (opens template and live file side-by-side for manual merge)"
            )
            print("[4] Discard modifications / Restore (restores file in next deployment)")
            print("[5] Skip file")
            choice = input("Select option [1-5]: ").strip()
            if choice in ["1", "2"]:
                return choose(
                    patch_and_edit(
                        src_file,
                        patch_content,
                        patch_from=install_file,
                        accept_conflicts=False,
                        open_editor=(choice == "2"),
                    ),
                    ItemAdoptOutcome.ADOPTED,
                    ItemAdoptOutcome.SKIPPED,
                )
            if choice == "3":
                return choose(
                    fallback_side_by_side(src_file, install_file),
                    ItemAdoptOutcome.ADOPTED,
                    ItemAdoptOutcome.SKIPPED,
                )
            if choice == "4":
                return ItemAdoptOutcome.ADOPTED
            return ItemAdoptOutcome.SKIPPED

        print(f"\nFound modified static config file (Patch applies cleanly): {rel_path}")
        if patch_content and patch_content.strip():
            print(f"{patch_content.strip()}\n")
        print("Reconciliation options:")
        print("[1] Adopt modifications (overwrites source file)")
        print("[2] Adopt and Edit in Editor (overwrites source file, then opens in $EDITOR)")
        print(
            "[3] Open Side-by-Side Reference (opens source file and live file side-by-side for manual merge)"
        )
        print("[4] Discard modifications / Restore (restores file in next deployment)")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice in ["1", "2"]:
            copy_file(install_file, src_file)
            _sync_file_mode(src_file, install_file)
            if choice == "2":
                try:
                    adopt_actions.launch_single_file_editor(src_file)
                except RuntimeError as e:
                    logger.warning(f"⚠️  Failed to open editor: {e}.")
                    return ItemAdoptOutcome.SKIPPED
            return ItemAdoptOutcome.ADOPTED
        if choice == "3":
            return choose(
                fallback_side_by_side(src_file, install_file),
                ItemAdoptOutcome.ADOPTED,
                ItemAdoptOutcome.SKIPPED,
            )
        if choice == "4":
            return ItemAdoptOutcome.ADOPTED
        return ItemAdoptOutcome.SKIPPED

    print(
        f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto file '{src_file.name}'!"
    )
    if patch_content and patch_content.strip():
        print(f"{patch_content.strip()}\n")
    print("Choose a fallback resolution strategy:")
    print("[1] Write Merge Conflicts (writes conflict markers to file)")
    print("[2] Open Merge Conflict Editor (writes conflict markers and opens editor)")
    print("[3] Open Side-by-Side Reference (opens file and static drift side-by-side)")
    print("[4] Over-render & Freeze (overwrites template/file, original saved to .bak)")
    print("[5] Discard modifications / Restore (restores file in next deployment)")
    print("[6] Skip file")
    choice = input("Select option [1-6]: ").strip()
    if choice in ["1", "2"]:
        return choose(
            patch_and_edit(
                src_file,
                patch_content,
                patch_from=install_file,
                accept_conflicts=True,
                open_editor=(choice == "2"),
            ),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.SKIPPED,
        )
    if choice == "3":
        return choose(
            fallback_side_by_side(src_file, install_file),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.SKIPPED,
        )
    if choice == "4":
        fallback_over_render(src_file, install_file)
        return ItemAdoptOutcome.ADOPTED
    return choose(choice == "5", ItemAdoptOutcome.ADOPTED, ItemAdoptOutcome.SKIPPED)


def handle_single_modification(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    install_base: Path,
    render_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    accept_conflicts: bool,
    force: bool = False,
    precomputed_patch: Optional[str] = None,
    src_match: Optional[RenderSourceMatch] = None,
    precomputed_conflict: Optional[bool] = None,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file/template modification."""
    if rel_path.name == DRIFT_KEEP_FILE_NAME:
        logger.info(f"Discarding unexpected modification to keep file '{rel_path}'.")
        ensure_empty_keep_file(install_pkg_dir / rel_path)
        return ItemAdoptOutcome.ADOPTED

    resolved_match = when_none(
        src_match,
        resolve_source_file_path(render_engines, src_dir_to_render, rel_path),
    )
    install_file = install_pkg_dir / rel_path

    if not resolved_match:
        return handle_single_addition(
            render_engines,
            src_pkg_dir,
            src_dir_to_render,
            install_pkg_dir,
            rel_path,
            interactive,
            force=force,
        )

    src_file = resolved_match.path
    assert_source_file_clean(src_file, force=force, pkg=pkg)

    is_templated = resolved_match.is_templated
    patch_content = branch(
        precomputed_patch is not None,
        lambda: precomputed_patch or "",
        lambda: _precompute_modification_patch(
            install_base=install_base,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            rel_mod=rel_path,
            src_match=resolved_match,
        ),
    )

    has_conflict = branch(
        precomputed_conflict is not None,
        lambda: bool(precomputed_conflict),
        lambda: check_patch_conflicts(src_file, patch_content),
    )

    return branch(
        not interactive,
        lambda: handle_modification_non_interactive(
            src_file,
            install_file,
            patch_content,
            is_templated,
            has_conflict,
            accept_conflicts,
        ),
        lambda: handle_modification_interactive(
            src_file,
            install_file,
            install_pkg_dir,
            rel_path,
            patch_content,
            is_templated,
            has_conflict,
        ),
    )
