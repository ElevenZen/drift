"""Unified Patch Generation, Conflict Probing, and Application Engine (Layer 1b).

Architecture & Call Chain Overview:
===================================
Provides cross-platform unified diff extraction, header rewriting, dry-run conflict
probing, and 3-way conflict-marker injection:

1. Unified Diff Extraction & Header Rewriting:
   - `check_patch_has_hunks`: Checks whether a diff string contains `'@@'` hunk headers.
   - `generate_install_git_patch`: Runs `git -C <install_base> diff HEAD -- <paths>`.
   - `rewrite_patch_target_headers`: Pure helper that strips pre-hunk Git metadata and rewrites
     `--- a/` and `+++ b/` headers to point at a target source file path.
   - `generate_retargeted_patch`: Composes `generate_install_git_patch` with
     `rewrite_patch_target_headers` to produce a source-targeted patch.
2. Conflict Probing & Patch Application:
   - `check_patch_conflicts`: Runs standard POSIX `patch --dry-run --no-backup-if-mismatch`.
   - `_parse_unified_patch_hunks`, `_apply_hunk_slice`, `_apply_patch_with_git_merge_file`:
     Parses unified diff hunks, synthesizes 3-way merge inputs (`current`, `base`, `other`),
     and invokes `git merge-file -L "" -L "" -L ""` for platform-independent conflict markers.
   - `apply_source_patch`: Applies clean patches via POSIX `patch --no-backup-if-mismatch`
     when `accept_conflicts=False`, and delegates unconditionally to
     `_apply_patch_with_git_merge_file` when `accept_conflicts=True`.
"""

from __future__ import annotations

from functools import reduce
import logging
from pathlib import Path
import re
import subprocess
import tempfile
from typing import List, Optional, Tuple

from ..utils.func_utils import choose

logger = logging.getLogger(__name__)

_UNIFIED_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# _RawHunk: (old_start, old_count, new_count, hunk_old_lines, hunk_new_lines)
_RawHunk = Tuple[int, int, int, List[str], List[str]]
# _ParsedHunk: (old_start, old_count, hunk_old_lines, hunk_new_lines)
_ParsedHunk = Tuple[int, int, List[str], List[str]]


def check_patch_has_hunks(patch_content: str) -> bool:
    """Checks if a unified diff string contains any actual text hunks (lines starting with '@@').

    Git diffs for file mode/permission-only changes (e.g. 'old mode 100644' / 'new mode 100755')
    or pure 100% similarity renames have non-empty headers but zero '@@' hunk blocks.
    """
    return any(line.startswith("@@") for line in patch_content.splitlines())


def generate_install_git_patch(
    install_base: Path,
    pkg_rel_path: Path,
    old_rel_path: Optional[Path] = None,
) -> str:
    """Generates the raw unified Git diff patch against HEAD for a drifted file in install/."""
    path_args = [
        *(
            [old_rel_path.as_posix()]
            if old_rel_path is not None
            else []
        ),
        pkg_rel_path.as_posix(),
    ]
    cmd = ["git", "-C", install_base.as_posix(), "diff", "HEAD", "--", *path_args]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return res.stdout


def _step_rewrite_patch_header_line(
    state: Tuple[bool, List[str]],
    line: str,
    patch_target_posix: str,
) -> Tuple[bool, List[str]]:
    """Pure reducer step that rewrites pre-hunk '--- '/'+++ ' headers and preserves hunk bodies."""
    in_hunk, acc = state
    if in_hunk:
        return (True, [*acc, line])
    if line.startswith("@@ ") or line == "@@":
        return (True, [*acc, line])
    if line.startswith("--- "):
        return (False, [*acc, f"--- a/{patch_target_posix}"])
    if line.startswith("+++ "):
        return (False, [*acc, f"+++ b/{patch_target_posix}"])
    return state


def rewrite_patch_target_headers(
    patch_content: str,
    patch_target_path: Path,
) -> str:
    """Strips pre-hunk Git metadata and rewrites '--- a/' and '+++ b/' headers to patch_target_path.

    Header lines are only stripped or rewritten before the first '@@' hunk header so that
    diff body lines starting with '--- ' (deleted '-- ' lines) or '+++ ' (added '++ ' lines)
    are preserved verbatim.
    """
    target_posix = patch_target_path.as_posix()
    if not patch_content.strip() or not target_posix:
        return patch_content

    _, rewritten_lines = reduce(
        lambda st, ln: _step_rewrite_patch_header_line(st, ln, target_posix),
        patch_content.splitlines(),
        (False, []),
    )
    return "\n".join(rewritten_lines) + "\n"


def generate_retargeted_patch(
    install_base: Path,
    pkg: str,
    new_rel_path: Path,
    old_rel_path: Path,
    patch_target_path: Path,
) -> str:
    """Generates a cross-file install/ Git diff patch and rewrites its diff headers to patch_target_path."""
    patch_content = generate_install_git_patch(
        install_base,
        Path(pkg) / new_rel_path,
        old_rel_path=Path(pkg) / old_rel_path,
    )
    return rewrite_patch_target_headers(patch_content, patch_target_path)


def check_patch_conflicts(src_file: Path, patch_content: str) -> bool:
    """Runs a dry-run of patch to determine if there are conflicts applying the patch to src_file.

    Note: The external patch tool (especially BSD patch on macOS) requires diff hunk blocks
    (lines starting with '@@') and errors if passed a hunkless diff (e.g. mode-only git diffs).
    If patch_content lacks '@@' hunks, returns False (no conflicts) immediately without calling patch.
    """
    if not check_patch_has_hunks(patch_content):
        return False
    cmd = ["patch", "--dry-run", "--no-backup-if-mismatch", src_file.as_posix()]
    try:
        subprocess.run(
            cmd,
            input=patch_content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return False
    except subprocess.CalledProcessError:
        return True


def _step_parse_hunk_line(
    state: Optional[Tuple[List[_RawHunk], Optional[_RawHunk]]],
    line: str,
) -> Optional[Tuple[List[_RawHunk], Optional[_RawHunk]]]:
    """Pure reducer step accumulating unified diff hunks and rejecting malformed hunk lines.

    Pre-hunk Git headers ('diff --git', 'index', '---', '+++') are ignored while cur is None.
    Once inside a hunk ('@@ ... @@'), each line must start with:
      - ' ': unchanged context line (present in both hunk_old and hunk_new)
      - '-': removed line (present in hunk_old only)
      - '+': added line (present in hunk_new only)
      - '\\': diff metadata comment (e.g. '\\ No newline at end of file', ignored)
    Any other line inside a hunk indicates a malformed patch and transitions state to None.
    """
    if state is None:
        return None
    hunks, cur = state
    m = _UNIFIED_HUNK_HEADER_RE.match(line)
    if m:
        next_hunks = [*hunks, *([cur] if cur is not None else [])]
        old_start = int(m.group(1))
        # In unified diffs, an omitted ',count' in '@@ -l +l @@' implies a line count of 1
        old_count = choose(m.group(2) is not None, int(m.group(2) or "0"), 1)
        new_count = choose(m.group(4) is not None, int(m.group(4) or "0"), 1)
        return (next_hunks, (old_start, old_count, new_count, [], []))
    if cur is None:
        return state
    old_s, old_c, new_c, h_old, h_new = cur
    if line.startswith(" "):
        return (hunks, (old_s, old_c, new_c, [*h_old, line[1:]], [*h_new, line[1:]]))
    if line.startswith("-"):
        return (hunks, (old_s, old_c, new_c, [*h_old, line[1:]], h_new))
    if line.startswith("+"):
        return (hunks, (old_s, old_c, new_c, h_old, [*h_new, line[1:]]))
    if line.startswith("\\"):
        return state
    return None


def _parse_unified_patch_hunks(
    patch_content: str,
) -> Optional[List[_ParsedHunk]]:
    """Parses unified diff hunks into a list of (old_start, old_count, hunk_old_lines, hunk_new_lines).

    Returns None if patch_content contains no hunks, mismatched line counts, or any malformed hunk line.
    """
    initial_state: Optional[Tuple[List[_RawHunk], Optional[_RawHunk]]] = ([], None)
    final_state = reduce(_step_parse_hunk_line, patch_content.splitlines(), initial_state)
    if final_state is None:
        return None
    raw_hunks, last_hunk = final_state
    all_hunks = [*raw_hunks, *([last_hunk] if last_hunk is not None else [])]
    # Validate that collected '-'/' ' and '+'/' ' line counts match the '@@ -old_s,old_c +new_s,new_c @@' header
    if not all_hunks or any(
        len(h_old) != old_c or len(h_new) != new_c
        for _, old_c, new_c, h_old, h_new in all_hunks
    ):
        return None
    return [(old_s, old_c, h_old, h_new) for old_s, old_c, _, h_old, h_new in all_hunks]


def _apply_hunk_slice(lines: List[str], hunk: _ParsedHunk, use_new: bool) -> List[str]:
    """Pure helper replacing a hunk's target slice [old_start - 1 : old_start - 1 + old_count]
    in lines with either hunk_old (when use_new=False, building the 3-way merge base) or
    hunk_new (when use_new=True, building the 3-way merge target).
    """
    old_start, old_count, h_old, h_new = hunk
    start_idx = max(0, old_start - 1)
    end_idx = start_idx + old_count
    replacement = [f"{l}\n" for l in choose(use_new, h_new, h_old)]
    return [*lines[:start_idx], *replacement, *lines[end_idx:]]


def _apply_patch_with_git_merge_file(src_file: Path, patch_content: str) -> bool:
    """Applies a unified diff patch with conflict markers via 'git merge-file'.

    Used unconditionally across all operating systems whenever accept_conflicts=True so
    conflict-marker injection behaves identically on Linux, macOS, and Windows without
    relying on GNU-only 'patch --merge' or BSD fallback branching.

    How it works:
    1. Parses all unified diff hunks from patch_content.
    2. Constructs synthetic 'base' and 'other' files by taking src_file's lines and replacing
       each hunk's line range (in reverse hunk order so earlier indices do not shift) with
       hunk_old (for 'base') and hunk_new (for 'other').
    3. Runs 'git merge-file -L "" -L "" -L "" <current_tmp> <base_tmp> <other_tmp>' inside a
       temporary directory so src_file is never mutated unless the merge succeeds.
    4. Writes the merged result (with bare '<<<<<<<', '=======', '>>>>>>>' conflict markers)
       back to src_file when git merge-file returns 0..127.
    """
    parsed_hunks = _parse_unified_patch_hunks(patch_content)
    if not parsed_hunks or not src_file.is_file():
        return False

    src_text = src_file.read_text(encoding="utf-8", errors="replace")
    src_lines = src_text.splitlines(keepends=True)
    # Apply hunks in reverse order so multi-hunk patches with line-count changes do not shift earlier hunk offsets
    rev_hunks = list(reversed(parsed_hunks))
    base_lines = reduce(lambda acc, h: _apply_hunk_slice(acc, h, False), rev_hunks, src_lines)
    other_lines = reduce(lambda acc, h: _apply_hunk_slice(acc, h, True), rev_hunks, src_lines)

    with tempfile.TemporaryDirectory() as tmp_dir:
        current_path = Path(tmp_dir) / "current"
        base_path = Path(tmp_dir) / "base"
        other_path = Path(tmp_dir) / "other"
        current_path.write_text(src_text, encoding="utf-8")
        base_path.write_text("".join(base_lines), encoding="utf-8")
        other_path.write_text("".join(other_lines), encoding="utf-8")
        # '--diff3' includes the '|||||||' baseline section alongside '<<<<<<<' (current/template)
        # and '=======' (other/drifted); three empty '-L ""' flags suppress file path labels.
        # Exit code: 0 = clean merge, 1..127 = number of conflict regions written, < 0 (or > 127) = fatal error.
        res = subprocess.run(
            [
                "git",
                "merge-file",
                "--diff3",
                "-L",
                "",
                "-L",
                "",
                "-L",
                "",
                current_path.as_posix(),
                base_path.as_posix(),
                other_path.as_posix(),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if not (0 <= res.returncode <= 127 and current_path.is_file()):
            return False
        merged_text = current_path.read_text(encoding="utf-8", errors="replace")
        if res.returncode > 0 and "<<<<<<<" not in merged_text:
            return False
        src_file.write_text(merged_text, encoding="utf-8")
        return True


def apply_source_patch(src_file: Path, patch_content: str, accept_conflicts: bool = False) -> bool:
    """Applies a patch to a source file, supporting merge markers if accept_conflicts is True.

    - If patch_content has no '@@' hunks (e.g. file mode/permission-only drift), returns True
      immediately without executing an external process.
    - When accept_conflicts is True, delegates unconditionally across all OSes to
      '_apply_patch_with_git_merge_file' ('git merge-file -L "" -L "" -L ""'), avoiding
      GNU-only 'patch --merge' and BSD patch compatibility branching.
    - When accept_conflicts is False, runs standard POSIX 'patch --no-backup-if-mismatch <src_file>'.
    """
    if not check_patch_has_hunks(patch_content):
        return True

    if accept_conflicts:
        return _apply_patch_with_git_merge_file(src_file, patch_content)

    cmd = ["patch", "--no-backup-if-mismatch", src_file.as_posix()]
    rej_file = src_file.with_suffix(src_file.suffix + ".rej")
    try:
        res = subprocess.run(
            cmd,
            input=patch_content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return res.returncode == 0 and not rej_file.exists()
    except (subprocess.CalledProcessError, OSError):
        return False
    finally:
        if rej_file.exists():
            rej_file.unlink()
