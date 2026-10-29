"""Interactive and Non-Interactive Single-Item Drift Dispatchers (Layer 3).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Addition Dispatch Pipeline (Expected State: target path absent & unblocked in src/):
        handle_single_addition(render_engines, src_pkg_dir, src_dir_to_render, install_pkg_dir, rel_path, interactive)
            - Directory Addition (_handle_single_directory_addition):
                * If directory already exists in src/ -> ensure_empty_keep_file -> returns ADOPTED
                * If find_source_path_conflict is not None -> reports conflict (CONFLICT) or prompts
                  _prompt_addition_collision_interactive -> _prompt_target_state_conflict_interactive
                * Non-interactive -> adopt_addition -> returns ADOPTED
                * Interactive -> _prompt_directory_addition_interactive ([1] Adopt, [2] Ignore, [3] Discard, [4] Skip)
            - File Addition (_handle_single_file_addition):
                * If find_source_path_conflict is not None -> reports conflict (CONFLICT) or prompts
                  _prompt_addition_collision_interactive -> _prompt_target_state_conflict_interactive
                * Non-interactive -> adopt_addition -> returns ADOPTED
                * Interactive -> _prompt_file_addition_interactive ([1] Adopt, [2] Ignore, [3] Discard, [4] Skip)
            -> Returns ItemAdoptOutcome

    2. Deletion Dispatch Pipeline (Expected State: target file/template or directory exists in src/):
        handle_single_deletion(render_engines, src_dir_to_render, rel_path, interactive)
            - Directory Deletion (_handle_single_directory_deletion):
                * Empty directory in src/ -> adopt_deletion (non-interactive) or _prompt_empty_directory_deletion_interactive
                * Non-empty directory in src/ -> discards deletion with warning (non-interactive) or
                  _prompt_non_empty_directory_deletion_interactive
            - File Deletion (_handle_single_file_deletion):
                * If resolve_source_file_path is None -> logs skip warning -> returns ADOPTED
                * Non-interactive -> adopt_deletion -> returns ADOPTED
                * Interactive -> _prompt_file_deletion_interactive ([1] Adopt, [2] Discard, [3] Skip)
            -> Returns ItemAdoptOutcome

    3. Rename Dispatch Pipeline (Expected State: old_rel_path exists in src/ & new_rel_path unblocked):
        handle_single_rename(render_engines, src_dir_to_render, old_rel_path, new_rel_path, patch_content, interactive, accept_conflicts)
            - Precondition Guard 1: verifies old_src_match exists in src/
                * On failure -> logs error (CONFLICT) or prompts _prompt_target_state_conflict_interactive
            - Precondition Guard 2: verifies find_source_path_conflict(new_rel_path) is None
                * On failure -> logs error (CONFLICT) or prompts _prompt_target_state_conflict_interactive
            - Non-Interactive (handle_rename_non_interactive):
                * Calls adopt_rename(..., accept_conflicts=accept_conflicts) -> ADOPTED or CONFLICT
            - Interactive (handle_rename_interactive):
                * Clean patch -> _prompt_rename_clean_interactive
                * Conflicting patch -> _prompt_rename_conflict_interactive
            -> Returns ItemAdoptOutcome

    4. Modification Dispatch Pipeline (Expected State: rel_path matches existing file/template in src/):
        handle_single_modification(render_engines, src_dir_to_render, install_pkg_dir, rel_path, patch_content, interactive, accept_conflicts)
            - Corrupt .drift_keep Guard -> ensure_empty_keep_file -> returns ADOPTED
            - Precondition Guard: verifies resolved_match exists in src/
                * On failure -> logs error (CONFLICT) or prompts _prompt_target_state_conflict_interactive
            - Non-Interactive (handle_modification_non_interactive):
                * Clean patch -> patch_and_edit / copy_file + _sync_file_mode -> ADOPTED or CONFLICT
                * Conflicting patch -> patch_and_edit(..., accept_conflicts=True) if accept_conflicts else CONFLICT
            - Interactive (handle_modification_interactive):
                * Conflicting patch -> _prompt_modification_conflict_interactive
                * Clean templated patch -> _prompt_templated_modification_clean_interactive
                * Clean static patch -> _prompt_static_modification_clean_interactive
            -> Returns ItemAdoptOutcome

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Shared Target-State Conflict & Display Helpers
        _prompt_target_state_conflict_interactive
        _prompt_addition_collision_interactive
        _extract_display_diff
    Layer 2: Category-Specific Interactive Prompt Menus & Non-Interactive Executors
        _prompt_directory_addition_interactive
        _prompt_file_addition_interactive
        _prompt_empty_directory_deletion_interactive
        _prompt_non_empty_directory_deletion_interactive
        _prompt_file_deletion_interactive
        handle_rename_non_interactive
        _prompt_rename_clean_interactive
        _prompt_rename_conflict_interactive
        handle_rename_interactive
        handle_modification_non_interactive
        _prompt_templated_modification_clean_interactive
        _prompt_static_modification_clean_interactive
        _prompt_modification_conflict_interactive
        handle_modification_interactive
    Layer 3: Top-Level Single-Item Dispatch Entry Points
        _handle_single_directory_addition
        _handle_single_file_addition
        handle_single_addition
        _handle_single_directory_deletion
        _handle_single_file_deletion
        handle_single_deletion
        handle_single_rename
        handle_single_modification
===============================================================================
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..config.render_engine_config import RenderEngineRegistry
from ..core.constants import DRIFT_KEEP_FILE_NAME
from ..models.adopt_models import AdoptPatch, ItemAdoptOutcome
from ..utils.file_ops import copy_file
from ..utils.func_utils import branch, choose, when_none
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
from .adopt_plan import (
    _translate_keep_to_dir,
    find_source_path_conflict,
    resolve_source_file_path,
)

logger = logging.getLogger(__name__)


def _prompt_target_state_conflict_interactive(
    conflict_message: str,
    discard_label: str,
    skip_label: str = "Skip file",
) -> ItemAdoptOutcome:
    """Prompts the user to discard/restore or skip when a source target is not in the expected state."""
    print(f"\n⚠️  [CONFLICT] {conflict_message}")
    print("Reconciliation options:")
    print(f"[1] {discard_label}")
    print(f"[2] {skip_label}")
    choice = input("Select option [1-2]: ").strip()
    return choose(choice == "1", ItemAdoptOutcome.ADOPTED, ItemAdoptOutcome.SKIPPED)


def _prompt_addition_collision_interactive(
    rel_path: Path, incoming_is_dir: bool
) -> ItemAdoptOutcome:
    """Prompts the user when an addition collides with an existing file/directory in source."""
    incoming_type = choose(incoming_is_dir, "directory", "file")
    existing_type = choose(incoming_is_dir, "file", "directory")
    return _prompt_target_state_conflict_interactive(
        conflict_message=f"Cannot adopt {incoming_type} '{rel_path}' because a {existing_type} with the same name exists in source!",
        discard_label=f"Discard addition / Restore (removes {incoming_type} on host next deployment)",
        skip_label=f"Skip {incoming_type}",
    )


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

    if find_source_path_conflict(render_engines, src_dir_to_render, target_rel) is not None:
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Cannot adopt directory '{target_rel}' because a file already exists at that path in source. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        outcome = _prompt_addition_collision_interactive(target_rel, incoming_is_dir=True)
        if outcome == ItemAdoptOutcome.ADOPTED:
            ensure_empty_keep_file(keep_file)
        return outcome

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
) -> ItemAdoptOutcome:
    """Handles adoption of a regular file addition."""
    conflict_match = find_source_path_conflict(render_engines, src_dir_to_render, rel_path)
    if conflict_match is not None:
        if not interactive:
            if conflict_match.path.is_dir():
                logger.error(
                    f"❌ [CONFLICT] Cannot adopt file '{rel_path}' because a directory already exists at that path in source. Skipping."
                )
            else:
                logger.error(
                    f"❌ [CONFLICT] Cannot adopt addition '{rel_path}' because the target already exists in source. Skipping."
                )
            return ItemAdoptOutcome.CONFLICT
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=False)

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
        ),
        lambda: _handle_single_file_addition(
            render_engines=render_engines,
            src_pkg_dir=src_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            install_pkg_dir=install_pkg_dir,
            rel_path=rel_path,
            interactive=interactive,
        ),
    )


def _prompt_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
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
        adopt_deletion(render_engines, src_dir_to_render, rel_path)
        return ItemAdoptOutcome.ADOPTED
    if choice == "2":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _prompt_non_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
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
            adopt_deletion(render_engines, src_dir_to_render, rel_path)
            return ItemAdoptOutcome.ADOPTED
        print("Aborted deletion adoption.")
        return ItemAdoptOutcome.SKIPPED
    return ItemAdoptOutcome.SKIPPED


def _prompt_file_deletion_interactive(
    render_engines: RenderEngineRegistry,
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
        adopt_deletion(render_engines, src_dir_to_render, rel_path)
        return ItemAdoptOutcome.ADOPTED
    if choice == "2":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _handle_single_directory_deletion(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a directory deletion."""
    is_empty = not any((src_dir_to_render / rel_path).iterdir())
    if not interactive:
        if is_empty:
            adopt_deletion(render_engines, src_dir_to_render, rel_path)
            return ItemAdoptOutcome.ADOPTED
        logger.warning(
            f"⚠️  [DISCARD] Cannot adopt directory deletion '{rel_path}' because the source directory is not empty. "
            f"Discarding deletion (will restore directory on next deploy)."
        )
        return ItemAdoptOutcome.ADOPTED

    return branch(
        is_empty,
        lambda: _prompt_empty_directory_deletion_interactive(
            render_engines, src_dir_to_render, rel_path
        ),
        lambda: _prompt_non_empty_directory_deletion_interactive(
            render_engines, src_dir_to_render, rel_path
        ),
    )


def _handle_single_file_deletion(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
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

    if not interactive:
        adopt_deletion(render_engines, src_dir_to_render, rel_path)
        return ItemAdoptOutcome.ADOPTED
    return _prompt_file_deletion_interactive(render_engines, src_dir_to_render, rel_path)


def handle_single_deletion(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file or directory deletion."""
    target_src = src_dir_to_render / rel_path
    return branch(
        target_src.is_dir() and not target_src.is_symlink(),
        lambda: _handle_single_directory_deletion(
            render_engines=render_engines,
            src_dir_to_render=src_dir_to_render,
            rel_path=rel_path,
            interactive=interactive,
        ),
        lambda: _handle_single_file_deletion(
            render_engines=render_engines,
            src_dir_to_render=src_dir_to_render,
            rel_path=rel_path,
            interactive=interactive,
        ),
    )


def _extract_display_diff(patch_content: Optional[AdoptPatch]) -> str:
    """Extracts the stripped human-readable diff string from an AdoptPatch."""
    if patch_content is None:
        return ""
    return patch_content.display_diff.strip()


def handle_rename_non_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: AdoptPatch,
    accept_conflicts: bool,
) -> ItemAdoptOutcome:
    """Processes a rename drift non-interactively."""
    display_diff = _extract_display_diff(patch_content)
    if display_diff:
        logger.debug(
            f"Diff for rename '{old_rel_path}' -> '{new_rel_path}':\n{display_diff}"
        )
    new_src_file = adopt_rename(
        render_engines,
        src_dir_to_render,
        old_rel_path,
        new_rel_path,
        patch_content,
        accept_conflicts=accept_conflicts,
    )
    return choose(
        new_src_file is not None,
        ItemAdoptOutcome.ADOPTED,
        ItemAdoptOutcome.CONFLICT,
    )


def _prompt_rename_clean_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: AdoptPatch,
) -> ItemAdoptOutcome:
    """Interactive menu for a rename whose patch applies cleanly."""
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
                fallback_side_by_side(new_src_file, patch_content.patch_from),
                ItemAdoptOutcome.ADOPTED,
                ItemAdoptOutcome.SKIPPED,
            )
        return ItemAdoptOutcome.ADOPTED
    if choice == "4":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _prompt_rename_conflict_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: AdoptPatch,
    src_file_name: str,
) -> ItemAdoptOutcome:
    """Interactive menu for a rename whose patch has conflicts."""
    file_kind = choose(patch_content.is_templated, "template file", "file")
    print(
        f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto renamed {file_kind} '{src_file_name}'!"
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
        effective_patch = choose(
            apply_conflict_patch,
            patch_content,
            AdoptPatch(
                src_match=patch_content.src_match,
                patch_from=patch_content.patch_from,
            ),
        )
        new_src_file = adopt_rename(
            render_engines,
            src_dir_to_render,
            old_rel_path,
            new_rel_path,
            patch_content=effective_patch,
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
                fallback_side_by_side(new_src_file, patch_content.patch_from),
                ItemAdoptOutcome.ADOPTED,
                ItemAdoptOutcome.SKIPPED,
            )
        if choice == "4":
            fallback_over_render(new_src_file, patch_content.patch_from)
            return ItemAdoptOutcome.ADOPTED

    return choose(choice == "5", ItemAdoptOutcome.ADOPTED, ItemAdoptOutcome.SKIPPED)


def handle_rename_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: AdoptPatch,
    src_file_name: Optional[str] = None,
) -> ItemAdoptOutcome:
    """Processes a rename drift interactively."""
    resolved_name = when_none(
        src_file_name,
        lambda: choose(
            patch_content.src_match is not None,
            patch_content.src_match.path.name if patch_content.src_match is not None else "",
            old_rel_path.name,
        ),
    )
    print(f"\nFound host file rename: {old_rel_path} -> {new_rel_path}")
    display_diff = _extract_display_diff(patch_content)
    if display_diff:
        print(f"{display_diff}\n")
    return branch(
        not patch_content.has_conflict,
        lambda: _prompt_rename_clean_interactive(
            render_engines,
            src_dir_to_render,
            old_rel_path,
            new_rel_path,
            patch_content,
        ),
        lambda: _prompt_rename_conflict_interactive(
            render_engines,
            src_dir_to_render,
            old_rel_path,
            new_rel_path,
            patch_content,
            src_file_name=resolved_name,
        ),
    )


def handle_single_rename(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: AdoptPatch,
    interactive: bool,
    accept_conflicts: bool,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file/template rename."""
    old_src_match = when_none(
        patch_content.src_match,
        lambda: resolve_source_file_path(render_engines, src_dir_to_render, old_rel_path),
    )
    if old_src_match is None or not old_src_match.path.exists():
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Cannot rename '{old_rel_path}' to '{new_rel_path}' because source file for '{old_rel_path}' was not found. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        return _prompt_target_state_conflict_interactive(
            conflict_message=f"Source file for '{old_rel_path}' was not found in source!",
            discard_label="Discard rename / Restore (restores original on host next deployment)",
        )

    target_conflict = find_source_path_conflict(
        render_engines, src_dir_to_render, new_rel_path
    )
    if target_conflict is not None:
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Cannot rename '{old_rel_path}' to '{new_rel_path}' because the target already exists in source. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        return _prompt_target_state_conflict_interactive(
            conflict_message=f"Target file '{new_rel_path}' already exists in source!",
            discard_label="Discard rename / Restore (restores original on host next deployment)",
        )

    return branch(
        not interactive,
        lambda: handle_rename_non_interactive(
            render_engines,
            src_dir_to_render,
            old_rel_path,
            new_rel_path,
            patch_content,
            accept_conflicts=accept_conflicts,
        ),
        lambda: handle_rename_interactive(
            render_engines,
            src_dir_to_render,
            old_rel_path,
            new_rel_path,
            patch_content,
            src_file_name=old_src_match.path.name,
        ),
    )


def handle_modification_non_interactive(
    src_file: Path,
    patch_content: AdoptPatch,
    accept_conflicts: bool,
) -> ItemAdoptOutcome:
    """Processes a modification drift non-interactively."""
    display_diff = _extract_display_diff(patch_content)
    if display_diff:
        logger.debug(f"Diff for '{src_file.name}':\n{display_diff}")
    if not patch_content.has_conflict:
        if patch_content.is_templated or patch_content.has_changes:
            return choose(
                patch_and_edit(
                    src_file,
                    patch_content,
                    accept_conflicts=False,
                    open_editor=False,
                ),
                ItemAdoptOutcome.ADOPTED,
                ItemAdoptOutcome.CONFLICT,
            )
        copy_file(patch_content.patch_from, src_file)
        _sync_file_mode(src_file, patch_content.patch_from)
        return ItemAdoptOutcome.ADOPTED

    if accept_conflicts:
        logger.warning(f"⚠️  Applying conflicting patch into file: '{src_file.name}'")
        return choose(
            patch_and_edit(
                src_file,
                patch_content,
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


def _prompt_templated_modification_clean_interactive(
    src_file: Path,
    rel_path: Path,
    patch_content: AdoptPatch,
) -> ItemAdoptOutcome:
    """Interactive menu for a templated modification whose patch applies cleanly."""
    display_diff = _extract_display_diff(patch_content)
    print(f"\nFound modified templated file (Patch applies cleanly): {rel_path}")
    if display_diff:
        print(f"{display_diff}\n")
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
                accept_conflicts=False,
                open_editor=(choice == "2"),
            ),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.SKIPPED,
        )
    if choice == "3":
        return choose(
            fallback_side_by_side(src_file, patch_content.patch_from),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.SKIPPED,
        )
    if choice == "4":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _prompt_static_modification_clean_interactive(
    src_file: Path,
    rel_path: Path,
    patch_content: AdoptPatch,
) -> ItemAdoptOutcome:
    """Interactive menu for a static file modification whose patch applies cleanly."""
    display_diff = _extract_display_diff(patch_content)
    print(f"\nFound modified static config file (Patch applies cleanly): {rel_path}")
    if display_diff:
        print(f"{display_diff}\n")
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
        copy_file(patch_content.patch_from, src_file)
        _sync_file_mode(src_file, patch_content.patch_from)
        if choice == "2":
            try:
                adopt_actions.launch_single_file_editor(src_file)
            except RuntimeError as e:
                logger.warning(f"⚠️  Failed to open editor: {e}.")
                return ItemAdoptOutcome.SKIPPED
        return ItemAdoptOutcome.ADOPTED
    if choice == "3":
        return choose(
            fallback_side_by_side(src_file, patch_content.patch_from),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.SKIPPED,
        )
    if choice == "4":
        return ItemAdoptOutcome.ADOPTED
    return ItemAdoptOutcome.SKIPPED


def _prompt_modification_conflict_interactive(
    src_file: Path,
    patch_content: AdoptPatch,
) -> ItemAdoptOutcome:
    """Interactive menu for a modification (static or templated) whose patch has conflicts."""
    display_diff = _extract_display_diff(patch_content)
    print(
        f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto file '{src_file.name}'!"
    )
    if display_diff:
        print(f"{display_diff}\n")
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
                accept_conflicts=True,
                open_editor=(choice == "2"),
            ),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.SKIPPED,
        )
    if choice == "3":
        return choose(
            fallback_side_by_side(src_file, patch_content.patch_from),
            ItemAdoptOutcome.ADOPTED,
            ItemAdoptOutcome.SKIPPED,
        )
    if choice == "4":
        fallback_over_render(src_file, patch_content.patch_from)
        return ItemAdoptOutcome.ADOPTED
    return choose(choice == "5", ItemAdoptOutcome.ADOPTED, ItemAdoptOutcome.SKIPPED)


def handle_modification_interactive(
    src_file: Path,
    rel_path: Path,
    patch_content: AdoptPatch,
) -> ItemAdoptOutcome:
    """Processes a modification drift interactively."""
    if patch_content.has_conflict:
        return _prompt_modification_conflict_interactive(src_file, patch_content)
    return branch(
        patch_content.is_templated,
        lambda: _prompt_templated_modification_clean_interactive(
            src_file, rel_path, patch_content
        ),
        lambda: _prompt_static_modification_clean_interactive(
            src_file, rel_path, patch_content
        ),
    )


def handle_single_modification(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    patch_content: AdoptPatch,
    interactive: bool,
    accept_conflicts: bool,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file/template modification."""
    if rel_path.name == DRIFT_KEEP_FILE_NAME:
        logger.info(f"Discarding unexpected modification to keep file '{rel_path}'.")
        ensure_empty_keep_file(install_pkg_dir / rel_path)
        return ItemAdoptOutcome.ADOPTED

    resolved_match = when_none(
        patch_content.src_match,
        lambda: resolve_source_file_path(render_engines, src_dir_to_render, rel_path),
    )
    if resolved_match is None or not resolved_match.path.exists():
        if not interactive:
            logger.error(
                f"❌ [CONFLICT] Source file for modification '{rel_path}' not found in source directory. Skipping."
            )
            return ItemAdoptOutcome.CONFLICT
        return _prompt_target_state_conflict_interactive(
            conflict_message=f"Source file for modification '{rel_path}' was not found in source!",
            discard_label="Discard modifications / Restore (restores file on host next deployment)",
        )

    src_file = resolved_match.path

    return branch(
        not interactive,
        lambda: handle_modification_non_interactive(
            src_file,
            patch_content,
            accept_conflicts,
        ),
        lambda: handle_modification_interactive(
            src_file,
            rel_path,
            patch_content,
        ),
    )
