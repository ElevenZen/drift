"""Unified Patch Generation, Parsing, Conflict Probing, and Application Engine (Layer 1b).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Raw Unified Diff Extraction (Planning Stage):
        - Tracked Modifications & Renames in install/:
            generate_install_git_patch(install_base, pkg_rel_path, old_rel_path) [Layer 1]
                - Executes 'git -C <install_base> diff HEAD -- <paths>'
                -> Returns raw unified diff string
        - Untracked Additions Promoted to Modifications:
            generate_source_to_install_patch(src_file, install_file) [Layer 1]
                - Executes 'git diff --no-index -- <src_file> <install_file>' (exit code 0 or 1)
                -> Returns raw unified diff string

    2. Pure Unified Diff Parsing into CleanPatchHunks (Planning Stage):
        parse_unified_patch_hunks(patch_content) [Layer 2]
            - Early Exit Guard (_has_hunk_headers): returns () if no '@@' hunk headers exist
            - Line-by-Line Hunk Accumulation (reduce over _step_parse_hunk_line):
                * Ignores pre-hunk Git headers ('diff --git', 'index', '---', '+++')
                * Parses '@@ -old_s,old_c +new_s,new_c @@' headers into _RawHunk accumulators
                * Collects newline-stripped ' ', '-', and '+' body lines into (h_old, h_new)
                * Tracks '\\ No newline at end of file' into (old_eof_nl, new_eof_nl) based on last_op
                * Raises ValueError on malformed '@@' headers or unrecognized hunk body prefixes
            - Header Line Count Validation:
                * Raises ValueError if len(h_old) != old_c or len(h_new) != new_c
            - 0-Based Slice Bound Resolution (_resolve_hunk_target_bounds):
                * Converts 1-based unified diff coordinates into 0-based [t_start, t_end) slices
                  (offset = 0 for pure insertions old_count == 0; offset = 1 for old_count > 0)
            -> Returns Tuple[CleanPatchHunk, ...]

    3. Pure-Python Dry-Run Conflict Probing (Planning & Execution Stages):
        check_patch_conflicts(src_file, patch_content) [Layer 3]
            - Returns True immediately if patch_content.conflict_clusters is non-empty or
              (patch_content.has_conflict and not patch_content.clean_hunks)
            - _check_patch_hunks_conflict(src_file, clean_hunks):
                * Reads src_file into newline-stripped src_lines and src_has_eof_nl
                * Probes every hunk via _check_hunk_matches(src_lines, hunk, src_has_eof_nl):
                    - _check_hunk_valid: verifies 0 <= t_start <= t_end and len(old_lines) == t_end - t_start
                    - Verifies src_lines[t_start:t_end] == old_lines
                    - When hunk touches EOF and is_old_eof_nl_valid, verifies src_has_eof_nl == old_eof_newline
            -> Returns bool (True if any hunk conflicts)

    4. Two-Step Pure-Python Patch Application (Execution Stage):
        apply_source_patch(src_file, patch_content, accept_conflicts) [Layer 4]
            - Guard: returns False if patch_content.has_conflict and not patch_content.has_changes
            - Templated Two-Step Path (when patch_content.conflict_clusters is non-empty):
                * Safeguard: raises SafeguardError if _check_patch_hunks_conflict(src_file, clean_hunks)
                  is True (Step 2 coordinates require Step 1 to apply cleanly)
                * Returns False if not accept_conflicts
                * Step 1 (T -> T_clean): _apply_step1_clean_hunks(src_file, clean_hunks, accept_conflicts=False)
                * Step 2 (T_clean -> T_merged): _apply_step2_conflict_clusters(src_file, conflict_clusters)
                    - Splices diff3 blocks ('<<<<<<< ', '||||||| ', '=======', '>>>>>>> ') in reverse
                      cluster order via _apply_single_conflict_cluster -> _format_conflict_cluster_diff3
                    - Writes joined text via _join_lines_with_eof_newline
            - Static Single-Step Path (when patch_content.conflict_clusters is empty):
                * Step 1: _apply_step1_clean_hunks(src_file, clean_hunks, accept_conflicts=accept_conflicts)
                    - Applies CleanPatchHunks in reverse order via _apply_hunk_to_lines:
                        * Clean match (_check_hunk_matches) -> replaces [t_start, t_end) with new_lines
                        * Mismatch with accept_conflicts=False -> raises ValueError (aborts with False)
                        * Mismatch with accept_conflicts=True -> splices 2-way conflict markers
                          ('<<<<<<< ', '=======', '>>>>>>> ') via _format_hunk_2way_conflict
                          (trimming shared prefix/suffix context via _common_context_prefix_len)
                    - Resolves output EOF newline via _resolve_step1_eof_newline and writes via
                      _join_lines_with_eof_newline
            -> Returns bool (True if applied and written)

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Raw Unified Diff Extraction
        generate_install_git_patch
        generate_source_to_install_patch
    Layer 2: Pure Unified Diff Hunk Parser
        _has_hunk_headers
        _step_parse_hunk_line
        _resolve_hunk_target_bounds
        parse_unified_patch_hunks
    Layer 3: Pure-Python Hunk Validation & Conflict Probing
        _check_hunk_valid
        _check_hunk_matches
        _check_patch_hunks_conflict
        check_patch_conflicts
    Layer 4: Two-Step Patch & Conflict-Marker Application Engine
        _common_context_prefix_len
        _format_hunk_2way_conflict
        _apply_hunk_to_lines
        _join_lines_with_eof_newline
        _resolve_step1_eof_newline
        _apply_step1_clean_hunks
        _format_conflict_cluster_diff3
        _apply_single_conflict_cluster
        _apply_step2_conflict_clusters
        apply_source_patch
===============================================================================
"""

from __future__ import annotations

from functools import reduce
import logging
from pathlib import Path
import re
import subprocess
from typing import List, Optional, Sequence, Tuple

from ..core.exceptions import SafeguardError
from ..models.adopt_models import AdoptPatch, CleanPatchHunk, ConflictCluster
from ..utils.func_utils import choose

logger = logging.getLogger(__name__)

_UNIFIED_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# _RawHunk: (old_start, old_count, new_count, hunk_old_lines, hunk_new_lines, old_eof_nl, new_eof_nl, last_op)
_RawHunk = Tuple[int, int, int, List[str], List[str], bool, bool, str]


def _has_hunk_headers(patch_text: str) -> bool:
    """Returns True if a raw unified diff string contains at least one '@@' hunk header."""
    return any(line.startswith("@@") for line in patch_text.splitlines())


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


def generate_source_to_install_patch(
    src_file: Path,
    install_file: Path,
) -> str:
    """Generates a unified diff directly from src_file to install_file via 'git diff --no-index'.

    Used when an untracked/newly added file in install/ (not in install/ HEAD) is promoted
    to a modification because a matching static file already exists at src_file.
    """
    cmd = [
        "git",
        "diff",
        "--no-index",
        "--",
        src_file.as_posix(),
        install_file.as_posix(),
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if res.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            res.returncode, cmd, output=res.stdout, stderr=res.stderr
        )
    return res.stdout


def _step_parse_hunk_line(
    state: Tuple[List[_RawHunk], Optional[_RawHunk]],
    line: str,
) -> Tuple[List[_RawHunk], Optional[_RawHunk]]:
    """Pure reducer step accumulating unified diff hunks from newline-stripped lines (`splitlines()`).

    Pre-hunk Git headers ('diff --git', 'index', '---', '+++') are ignored while `cur` is None.
    Once inside a hunk ('@@ ... @@'), each newline-stripped `line` must start with:
      - ' ': unchanged context line (appends `line[1:]` to both `h_old` and `h_new`)
      - '-': removed line (appends `line[1:]` to `h_old`)
      - '+': added line (appends `line[1:]` to `h_new`)
      - '\\': diff metadata comment (e.g. '\\ No newline at end of file', sets `old_eof_nl=False`
              when following '-' or ' ', and sets `new_eof_nl=False` when following '+' or ' ')
    Any other line inside a hunk indicates a malformed patch and raises ValueError.
    """
    hunks, cur = state
    if line.startswith("@@"):
        m = _UNIFIED_HUNK_HEADER_RE.match(line)
        if m is None:
            raise ValueError(f"Malformed unified diff hunk header: {line!r}")
        old_start = int(m.group(1))
        # In unified diffs, an omitted ',count' in '@@ -l +l @@' implies a line count of 1
        old_count = choose(m.group(2) is not None, int(m.group(2) or "0"), 1)
        new_count = choose(m.group(4) is not None, int(m.group(4) or "0"), 1)
        return (
            [*hunks, *(choose(cur is not None, [cur], []))],
            (old_start, old_count, new_count, [], [], True, True, ""),
        )
    if cur is None:
        return state
    old_s, old_c, new_c, h_old, h_new, old_eof_nl, new_eof_nl, last_op = cur
    if line.startswith(" "):
        return (
            hunks,
            (
                old_s,
                old_c,
                new_c,
                [*h_old, line[1:]],
                [*h_new, line[1:]],
                old_eof_nl,
                new_eof_nl,
                " ",
            ),
        )
    if line.startswith("-"):
        return (
            hunks,
            (old_s, old_c, new_c, [*h_old, line[1:]], h_new, old_eof_nl, new_eof_nl, "-"),
        )
    if line.startswith("+"):
        return (
            hunks,
            (old_s, old_c, new_c, h_old, [*h_new, line[1:]], old_eof_nl, new_eof_nl, "+"),
        )
    if line.startswith("\\"):
        next_old_eof = choose(last_op in ("-", " "), False, old_eof_nl)
        next_new_eof = choose(last_op in ("+", " "), False, new_eof_nl)
        return (
            hunks,
            (old_s, old_c, new_c, h_old, h_new, next_old_eof, next_new_eof, "\\"),
        )
    raise ValueError(f"Malformed unified diff line inside hunk: {line!r}")


def _resolve_hunk_target_bounds(old_start: int, old_count: int) -> Tuple[int, int]:
    """Computes the 0-based [start_idx, end_idx) slice bounds for a unified diff hunk.

    In GNU unified diff format:
    - Deletion or replacement (`old_count > 0`): `@@ -k,c +... @@` starts *at* 1-based
      line `k`, which maps to 0-based index `k - 1` (offset = 1).
    - Pure insertion (`old_count == 0`): `@@ -k,0 +... @@` inserts *after* 1-based
      line `k` (or at the start of an empty file when `k == 0`), which maps to
      0-based slice `[k, k)` (offset = 0).
    """
    start_idx = choose(old_count == 0, max(0, old_start), max(0, old_start - 1))
    return (start_idx, start_idx + old_count)


def parse_unified_patch_hunks(
    patch_content: str,
) -> Tuple[CleanPatchHunk, ...]:
    """Pure parser converting a unified diff string into a tuple of 0-based `CleanPatchHunk` instances.

    - Returns `()` when `patch_content` has no `'@@'` hunk headers (e.g., empty diff,
      mode-only change, or 100% similarity rename).
    - Raises `ValueError` when `patch_content` contains malformed `'@@'` headers,
      unrecognized hunk body prefixes, or mismatched line counts.
    """
    if not _has_hunk_headers(patch_content):
        return ()
    # Parse the unified diff into a list of raw hunks, each represented as a tuple
    initial_state: Tuple[List[_RawHunk], Optional[_RawHunk]] = ([], None)
    raw_hunks, last_hunk = reduce(
        _step_parse_hunk_line, patch_content.splitlines(), initial_state
    )
    all_hunks = [*raw_hunks, *(choose(last_hunk is not None, [last_hunk], []))]
    # Validate that collected '-'/' ' and '+'/' ' line counts match the '@@ -old_s,old_c +new_s,new_c @@' header
    if not all_hunks or any(
        len(h_old) != old_c or len(h_new) != new_c
        for _, old_c, new_c, h_old, h_new, _, _, _ in all_hunks
    ):
        raise ValueError("Malformed unified diff: hunk line counts do not match '@@' header.")
    # Convert each raw hunk into a CleanPatchHunk with 0-based [t_start, t_end) slice bounds
    return tuple(
        CleanPatchHunk(
            t_start=start_idx,
            t_end=end_idx,
            old_lines=tuple(h_old),
            new_lines=tuple(h_new),
            old_eof_newline=old_eof_nl,
            new_eof_newline=new_eof_nl,
        )
        for old_s, old_c, _, h_old, h_new, old_eof_nl, new_eof_nl, _ in all_hunks
        for start_idx, end_idx in (_resolve_hunk_target_bounds(old_s, old_c),)
    )


def _check_hunk_valid(hunk: CleanPatchHunk) -> bool:
    """Checks the structural slice invariant (`0 <= t_start <= t_end` and `len(old_lines) == t_end - t_start`)."""
    return (
        0 <= hunk.t_start <= hunk.t_end
        and len(hunk.old_lines) == hunk.t_end - hunk.t_start
    )


def _check_hunk_matches(
    src_lines: Sequence[str],
    hunk: CleanPatchHunk,
    src_has_eof_nl: bool = True,
) -> bool:
    """Checks whether a single `CleanPatchHunk` matches newline-stripped `src_lines` at `[t_start, t_end)`."""
    if not _check_hunk_valid(hunk):
        return False
    hunk_at_eof = hunk.t_end == len(src_lines)
    eof_nl_matches = choose(
        hunk.is_old_eof_nl_valid and hunk_at_eof,
        src_has_eof_nl == hunk.old_eof_newline,
        True,
    )
    return (
        eof_nl_matches
        and hunk.t_end <= len(src_lines)
        and list(src_lines[hunk.t_start : hunk.t_end]) == list(hunk.old_lines)
    )


def _check_patch_hunks_conflict(
    src_file: Path,
    clean_hunks: Sequence[CleanPatchHunk],
) -> bool:
    """Pure-Python conflict probe for a sequence of `CleanPatchHunk`s against `src_file`."""
    if not clean_hunks:
        return False
    if not src_file.is_file():
        return True
    try:
        src_text = src_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    src_has_eof_nl = src_text.endswith(("\n", "\r"))
    src_lines = src_text.splitlines()
    return any(
        not _check_hunk_matches(src_lines, hunk, src_has_eof_nl=src_has_eof_nl)
        for hunk in clean_hunks
    )


def check_patch_conflicts(
    src_file: Path,
    patch_content: AdoptPatch,
) -> bool:
    """Determines whether applying `patch_content` to `src_file` has any conflicts.

    Returns `True` immediately if `patch_content.has_conflict` or `patch_content.conflict_clusters`
    is truthy; otherwise probes `patch_content.clean_hunks` in pure Python against `src_file`.
    """
    if patch_content.conflict_clusters or (
        patch_content.has_conflict and not patch_content.clean_hunks
    ):
        return True
    return _check_patch_hunks_conflict(src_file, patch_content.clean_hunks)


def _common_context_prefix_len(
    a: Sequence[str],
    b: Sequence[str],
    c: Sequence[str],
) -> int:
    """Counts leading lines that are identical across all three line sequences."""
    limit = min(len(a), len(b), len(c))
    mismatches = [i for i in range(limit) if not (a[i] == b[i] == c[i])]
    return choose(bool(mismatches), mismatches[0], limit)


def _format_hunk_2way_conflict(
    actual_slice: Sequence[str],
    h_old: Sequence[str],
    h_new: Sequence[str],
) -> List[str]:
    """Formats a mismatched static-file hunk into newline-stripped 2-way conflict marker lines
    (`<<<<<<< `, `=======`, `>>>>>>> `), preserving any unchanged leading or trailing context lines.
    """
    prefix_len = _common_context_prefix_len(actual_slice, h_old, h_new)
    suffix_len = _common_context_prefix_len(
        list(reversed(actual_slice[prefix_len:])),
        list(reversed(h_old[prefix_len:])),
        list(reversed(h_new[prefix_len:])),
    )
    actual_end = len(actual_slice) - suffix_len
    new_end = len(h_new) - suffix_len
    prefix_lines = actual_slice[:prefix_len]
    suffix_lines = actual_slice[actual_end:]
    actual_core = actual_slice[prefix_len:actual_end]
    new_core = h_new[prefix_len:new_end]
    return [
        *prefix_lines,
        "<<<<<<< ",
        *actual_core,
        "=======",
        *new_core,
        ">>>>>>> ",
        *suffix_lines,
    ]


def _apply_hunk_to_lines(
    lines: List[str],
    hunk: CleanPatchHunk,
    accept_conflicts: bool,
    src_has_eof_nl: bool = True,
) -> List[str]:
    """Applies a single `CleanPatchHunk` to newline-stripped `lines` at `[t_start, t_end)`.

    When the hunk matches `lines`, replaces `[t_start, t_end)` with `hunk.new_lines`.
    When the hunk mismatches and `accept_conflicts=True`, replaces `[t_start, t_end)`
    with a 2-way conflict marker block (`<<<<<<< `, `=======`, `>>>>>>> `).
    Raises `ValueError` when the hunk is structurally invalid or mismatches with `accept_conflicts=False`.
    """
    if not _check_hunk_valid(hunk):
        raise ValueError(
            f"Invalid CleanPatchHunk bounds: [{hunk.t_start}, {hunk.t_end})"
        )
    start_idx, end_idx = hunk.t_start, hunk.t_end
    if _check_hunk_matches(
        lines,
        hunk,
        src_has_eof_nl=src_has_eof_nl,
    ):
        return [*lines[:start_idx], *hunk.new_lines, *lines[end_idx:]]
    if not accept_conflicts:
        raise ValueError(
            f"CleanPatchHunk mismatch at [{start_idx}, {end_idx}) with accept_conflicts=False"
        )
    actual_slice = list(lines[start_idx:end_idx])
    conflict_replacement = _format_hunk_2way_conflict(
        actual_slice, hunk.old_lines, hunk.new_lines
    )
    return [*lines[:start_idx], *conflict_replacement, *lines[end_idx:]]


def _join_lines_with_eof_newline(
    lines: Sequence[str],
    eof_newline: bool,
) -> str:
    """Joins newline-stripped `lines` and appends a trailing newline when `eof_newline` is True."""
    return choose(
        bool(lines),
        "\n".join(lines) + choose(eof_newline, "\n", ""),
        "",
    )


def _resolve_step1_eof_newline(
    src_lines: Sequence[str],
    last_hunk: CleanPatchHunk,
    src_has_eof_nl: bool,
) -> bool:
    """Resolves the output file's trailing newline state after applying Step 1 `clean_hunks`."""
    hunk_at_eof = last_hunk.t_end == len(src_lines)
    last_hunk_active = hunk_at_eof and _check_hunk_matches(
        src_lines,
        last_hunk,
        src_has_eof_nl=src_has_eof_nl,
    )
    return choose(
        hunk_at_eof,
        choose(
            last_hunk_active,
            choose(last_hunk.is_new_eof_nl_valid, last_hunk.new_eof_newline, True),
            True,  # fallback to True if last hunk conflicted and accept_conflicts=True (cannot determine new EOF state)
        ),
        src_has_eof_nl,
    )


def _apply_step1_clean_hunks(
    src_file: Path,
    clean_hunks: Sequence[CleanPatchHunk],
    accept_conflicts: bool = False,
) -> bool:
    """Applies structured `CleanPatchHunk` slices to `src_file` in reverse order."""
    if not clean_hunks:
        return True
    if not src_file.is_file():
        return False
    try:
        src_text = src_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    src_has_eof_nl = src_text.endswith(("\n", "\r"))
    src_lines = src_text.splitlines()
    rev_hunks = list(reversed(clean_hunks))
    try:
        merged_lines = reduce(
            lambda cur_lines, h: _apply_hunk_to_lines(
                cur_lines,
                h,
                accept_conflicts,
                src_has_eof_nl=src_has_eof_nl,
            ),
            rev_hunks,
            src_lines,
        )
    except ValueError:
        return False

    final_eof_nl = _resolve_step1_eof_newline(
        src_lines,
        clean_hunks[-1],
        src_has_eof_nl,
    )
    out_text = _join_lines_with_eof_newline(merged_lines, final_eof_nl)
    try:
        src_file.write_text(out_text, encoding="utf-8")
        return True
    except OSError:
        return False


def _format_conflict_cluster_diff3(cluster: ConflictCluster) -> List[str]:
    """Formats a single 3-way ConflictCluster into newline-stripped diff3 conflict marker lines."""
    return [
        "<<<<<<< ",
        *(ln.rstrip("\r\n") for ln in cluster.t_lines),
        "||||||| ",
        *(ln.rstrip("\r\n") for ln in cluster.r_lines),
        "=======",
        *(ln.rstrip("\r\n") for ln in cluster.h_lines),
        ">>>>>>> ",
    ]


def _apply_single_conflict_cluster(
    lines: List[str],
    cluster: ConflictCluster,
) -> List[str]:
    """Replaces newline-stripped `lines[cluster.t_clean_start : cluster.t_clean_end]` with its diff3 block."""
    return [
        *lines[: cluster.t_clean_start],
        *_format_conflict_cluster_diff3(cluster),
        *lines[cluster.t_clean_end :],
    ]


def _apply_step2_conflict_clusters(
    src_file: Path,
    conflict_clusters: Sequence[ConflictCluster],
) -> bool:
    """Injects Step 2 3-way conflict clusters onto `T_clean` (`src_file` after Step 1) in reverse order."""
    if not conflict_clusters:
        return True
    if not src_file.is_file():
        return False

    t_clean_text = src_file.read_text(encoding="utf-8", errors="replace")
    has_eof_nl = t_clean_text.endswith(("\n", "\r"))
    t_clean_lines = t_clean_text.splitlines()
    rev_clusters = list(reversed(conflict_clusters))
    merged_lines = reduce(
        _apply_single_conflict_cluster,
        rev_clusters,
        t_clean_lines,
    )
    # step 2 always conflicts,
    # so always add a trailing newline if the last cluster touches EOF,
    # otherwise preserve the original EOF state
    touches_eof = conflict_clusters[-1].t_clean_end == len(t_clean_lines)
    final_eof_nl = choose(touches_eof, True, has_eof_nl)
    out_text = _join_lines_with_eof_newline(merged_lines, final_eof_nl)
    src_file.write_text(out_text, encoding="utf-8")
    return True


def apply_source_patch(
    src_file: Path,
    patch_content: AdoptPatch,
    accept_conflicts: bool = False,
) -> bool:
    """Applies a structured `AdoptPatch` in two steps to `src_file` in pure Python.

    Two-Step Execution for `AdoptPatch`:
    1. Step 1 (Clean Hunks: `T -> T_clean`):
       Applies `patch_content.clean_hunks` directly in Python.
       - If `patch_content.conflict_clusters` is non-empty and `clean_hunks` fails to
         match `src_file`, raises `SafeguardError` (because Step 2's `[t_clean_start, t_clean_end)`
         coordinates depend on Step 1 applying cleanly).
       - If `patch_content.conflict_clusters` is empty and `clean_hunks` has a conflict on
         a static file, writes 2-way conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) when
         `accept_conflicts=True`, or returns `False` when `accept_conflicts=False`.
    2. Step 2 (Conflict Clusters: `T_clean -> T_merged`):
       When `conflict_clusters` is non-empty and `accept_conflicts=True`, injects 3-way `diff3`
       conflict blocks (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`) at `[t_clean_start, t_clean_end)`
       in reverse cluster order so adjacent clean edits and conflict anchors never collide.
    """
    if patch_content.has_conflict and not patch_content.has_changes:
        return False
    if patch_content.conflict_clusters:
        if _check_patch_hunks_conflict(src_file, patch_content.clean_hunks):
            raise SafeguardError(
                f"Step 1 clean hunks failed to match '{src_file}' while Step 2 "
                "conflict clusters are present."
            )
        if not accept_conflicts:
            return False
        if not _apply_step1_clean_hunks(
            src_file, patch_content.clean_hunks, accept_conflicts=False
        ):
            return False
        return _apply_step2_conflict_clusters(
            src_file, patch_content.conflict_clusters
        )

    return _apply_step1_clean_hunks(
        src_file,
        patch_content.clean_hunks,
        accept_conflicts=accept_conflicts,
    )
