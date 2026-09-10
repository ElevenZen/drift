"""Utilities for launching external editors and visual diff tools."""

import os
import shlex
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Sequence, Tuple, List, Union

logger = logging.getLogger(__name__)

SUPPORTED_EDITORS = ("nvim", "vim", "code", "codium", "code-oss", "code-insiders", "emacs")


def get_configured_editor() -> str:
    """
    Returns the configured $EDITOR command string.
    Raises RuntimeError if $EDITOR is unset or empty.
    """
    editor_env = os.environ.get("EDITOR", "").strip()
    if not editor_env:
        raise RuntimeError(
            "Environment variable $EDITOR is not set.\n"
            "Please set $EDITOR (e.g. 'export EDITOR=nvim' or 'export EDITOR=code') to use editor features."
        )
    return editor_env


def parse_editor_command(editor_cmd: str) -> Tuple[List[str], str]:
    """
    Parses the configured $EDITOR string into a list of command tokens and the editor base name.
    Uses shlex.split to properly parse quotes, spaces, and flags.
    """
    tokens = shlex.split(editor_cmd)
    if not tokens:
        raise RuntimeError(
            "Environment variable $EDITOR is invalid or empty.\n"
            "Please set $EDITOR (e.g. 'export EDITOR=nvim' or 'export EDITOR=code') to use editor features."
        )
    editor_name = Path(tokens[0]).name.lower()
    return tokens, editor_name


def launch_single_file_editor(file_path: Path) -> None:
    """
    Opens a single file in the configured $EDITOR.
    Supports nvim, vim, code / codium / code-oss / code-insiders (--wait), emacs, and generic executable editors.
    Raises RuntimeError if $EDITOR is unset or invalid.
    """
    editor_cmd = get_configured_editor()
    tokens, editor_name = parse_editor_command(editor_cmd)
    editor_bin = tokens[0]

    if editor_name in ("code", "codium", "code-oss", "code-insiders"):
        cmd = tokens + ["--wait", str(file_path)]
    else:
        cmd = tokens + [str(file_path)]

    logger.info(f"📝 Launching editor '{editor_bin}' for '{file_path.name}'...")
    logger.info(f"⏳ Waiting for '{editor_bin}' to close '{file_path.name}'...")

    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        raise RuntimeError(f"Editor executable '{editor_bin}' not found.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Editor '{editor_bin}' exited with error code {e.returncode}.")


def _to_tokens(editor_spec: Union[str, Sequence[str]]) -> List[str]:
    """Helper to convert string or sequence into a list of command tokens."""
    if isinstance(editor_spec, str):
        return shlex.split(editor_spec)
    return list(editor_spec)


def launch_vim_diff(editor_spec: Union[str, Sequence[str]], file_pairs: Sequence[Tuple[Path, Path]]) -> None:
    """Launches Neovim or Vim in a single session with tabpages and vertical diffsplit per tab."""
    editor_tokens = _to_tokens(editor_spec)
    editor_name = Path(editor_tokens[0]).name
    first_left, first_right = file_pairs[0]
    cmd = editor_tokens + [str(first_left), "-c", f"vert diffsplit {first_right}"]
    for left, right in file_pairs[1:]:
        cmd.extend(["-c", f"tabe {left}", "-c", f"vert diffsplit {right}"])
    if len(file_pairs) > 1:
        cmd.extend(["-c", "tabfirst"])
    logger.info(f"📝 Launching {editor_name} with {len(file_pairs)} diff pair(s) across tabpages...")
    logger.info(f"⏳ Waiting for '{editor_tokens[0]}' diff session to be closed...")
    subprocess.run(cmd, check=False)


def launch_vscode_diff(editor_spec: Union[str, Sequence[str]], file_pairs: Sequence[Tuple[Path, Path]]) -> None:
    """Launches VS Code / VSCodium / Code-OSS opening all diff tabs in the active window."""
    editor_tokens = _to_tokens(editor_spec)
    editor_name = Path(editor_tokens[0]).name
    logger.info(f"📝 Opening {len(file_pairs)} diff tab(s) in {editor_name}...")
    for left, right in file_pairs[:-1]:
        subprocess.run(editor_tokens + ["--diff", str(left), str(right), "--reuse-window"], check=False)
    last_left, last_right = file_pairs[-1]
    logger.info(f"⏳ Waiting for '{editor_tokens[0]}' diff session to be closed...")
    subprocess.run(editor_tokens + ["--diff", str(last_left), str(last_right), "--reuse-window", "--wait"], check=False)


def launch_emacs_diff(editor_spec: Union[str, Sequence[str]], file_pairs: Sequence[Tuple[Path, Path]]) -> None:
    """Launches GNU Emacs in a single session with side-by-side ediff and tab-bar-mode per tab."""
    editor_tokens = _to_tokens(editor_spec)
    logger.info(f"📝 Launching Emacs with {len(file_pairs)} diff tab(s)...")
    steps = [
        "(setq ediff-split-window-function 'split-window-horizontally)",
        "(setq ediff-window-setup-function 'ediff-setup-windows-plain)",
        "(setq auto-save-default nil)",
        "(setq make-backup-files nil)",
        "(setq create-lockfiles nil)",
    ]
    if len(file_pairs) == 1:
        steps.append(f'(ediff-files "{file_pairs[0][0]}" "{file_pairs[0][1]}")')
    else:
        steps.append("(tab-bar-mode 1)")
        for i, (left, right) in enumerate(file_pairs):
            if i > 0:
                steps.append("(tab-new)")
            steps.append(f'(ediff-files "{left}" "{right}")')
        steps.append("(tab-bar-select-tab 1)")
    elisp = f"(progn {' '.join(steps)})"
    cmd = editor_tokens + ["--eval", elisp]
    logger.info(f"⏳ Waiting for '{editor_tokens[0]}' diff session to be closed...")
    subprocess.run(cmd, check=False)


def launch_side_by_side_editor(file_pairs: Sequence[Tuple[Path, Path]]) -> None:
    """
    Opens side-by-side multi-tab diff comparison for all file pairs in a single editor session.
    Delegates to the appropriate editor adapter.
    """
    if not file_pairs:
        logger.info("✨ No differences detected between layers.")
        return

    editor_cmd = get_configured_editor()
    tokens, editor_name = parse_editor_command(editor_cmd)

    if editor_name in ("nvim", "vim"):
        launch_vim_diff(tokens, file_pairs)
    elif editor_name in ("code", "codium", "code-oss", "code-insiders"):
        launch_vscode_diff(tokens, file_pairs)
    elif editor_name == "emacs":
        launch_emacs_diff(tokens, file_pairs)
    else:
        raise RuntimeError(
            f"Editor '{editor_name}' is not supported for side-by-side diffing (-y).\n"
            f"Supported editors: {', '.join(SUPPORTED_EDITORS)}."
        )
