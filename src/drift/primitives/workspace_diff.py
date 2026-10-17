"""Primitive 15: Change Visualization (Diff A, B, and Δ / Pending).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 5: Primitive Entry Point
    run_primitive_15_workspace_diff(workspace_config, package_names, diff_type, side_by_side, stat, quiet)
        State Synchronization:
            run_primitive_1_reverse_sync
            run_primitive_2_render_packages

        Structured Diff Detail Collection:
            collect_git_repo_diff_details(repo_path, pkg) [Layer 3]
                parse_git_status_porcelain(repo_path, pkg)
                    get_git_status_porcelain(repo_path, pkg_path) -> check_is_trackable
            collect_pending_folder_diff_details(workspace_config, pkg) [Layer 3]
                compare_folders / tree_files -> check_is_trackable

        Interactive / Visual Mode (side_by_side=True):
            run_side_by_side_diff(workspace_config, packages, diff_type) [Layer 4]
                collect_repo_diff_pairs(repo_path, packages, temp_dir) [Layer 2]
                    check_is_trackable / ensure_empty_file [Layer 1]
                collect_pending_delta_pairs(workspace_config, packages, temp_dir) [Layer 2]
                    get_pending_delta_worklist(workspace_config, packages) [Layer 1]
                    compare_folders / tree_files -> check_is_trackable / ensure_empty_file [Layer 1]
                launch_side_by_side_editor(pairs)

        Standard Terminal Mode (side_by_side=False):
            run_terminal_diff(workspace_config, packages, diff_type, stat) [Layer 4]
                run_repo_diff(repo_path, packages, git_options) [Layer 3]
                    filter_diff_output(diff_text) [Layer 3]
                run_pending_delta_diff(workspace_config, packages, git_options) [Layer 3]
                    get_pending_delta_worklist(workspace_config, packages) [Layer 1]
                    filter_diff_output(diff_text) [Layer 3]
                        is_diff_output_chunk_candidate(chunk)
                            extract_paths_from_diff_header(first_line)
                            check_is_trackable
                        is_stat_line_candidate(line)
                            check_is_trackable

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Worklist Classification & Temp File Helpers
        get_pending_delta_worklist
        ensure_empty_file
    Layer 2: Side-by-Side Diff Pair Collectors
        collect_repo_diff_pairs
        collect_pending_delta_pairs
    Layer 3: Structured Diff Collectors, Output Filters & Terminal Git Diff Runners
        collect_git_repo_diff_details
        collect_pending_folder_diff_details
        extract_paths_from_diff_header
        is_diff_output_chunk_candidate
        is_stat_line_candidate
        filter_diff_output
        run_repo_diff
        run_pending_delta_diff
    Layer 4: Diff Strategy Dispatchers
        run_side_by_side_diff
        run_terminal_diff
    Layer 5: Public Primitive Entry Point
        run_primitive_15_workspace_diff
===============================================================================
"""

import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple, Sequence, Iterable, Optional

from ..core.constants import DirMode
from ..config.workspace_config import WorkspaceConfig
from ..models import WorkspaceDiffType, DiffResult, PackageDiffDetail, FileDiffDetail
from ..core.folder_diff import compare_folders
from ..utils.file_inspect import check_is_trackable
from ..utils.git_utils import parse_git_status_porcelain, unquote_git_path
from ..utils.editor_utils import launch_side_by_side_editor

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Worklist Classification Helpers
# =====================================================================

def get_pending_delta_worklist(
    workspace_config: WorkspaceConfig,
    packages: Iterable[str]
) -> Tuple[List[Tuple[str, Path, Path]], List[str], List[str]]:
    """
    Classifies packages based on their presence in render/ and install/ directories.
    Returns (to_diff_list, new_package_names, orphan_package_names).
    to_diff_list contains (package_names, install_pkg_dir, render_pkg_dir) .
    """
    to_diff = []
    new_pkgs = []
    orphan_pkgs = []

    for pkg in packages:
        # Use paths relative to drift_root for diffing
        rel_install = workspace_config.workspace.install_directory / pkg
        rel_render = workspace_config.workspace.render_directory / pkg

        abs_install = (workspace_config.install_path / pkg).resolve()
        abs_render = (workspace_config.render_path / pkg).resolve()

        if abs_install.exists() and abs_render.exists():
            to_diff.append((pkg, rel_install, rel_render))
        elif abs_render.exists():
            new_pkgs.append(pkg)
        elif abs_install.exists():
            orphan_pkgs.append(pkg)

    return to_diff, new_pkgs, orphan_pkgs


def ensure_empty_file(target_path: Path) -> Path:
    """Creates parent directories and touches an empty file at target_path, returning target_path."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.touch()
    return target_path



# =====================================================================
# Layer 2: Side-by-Side Diff Pair Collectors
# =====================================================================

def collect_repo_diff_pairs(
    repo_path: Path,
    packages: Sequence[str],
    temp_dir: Path,
) -> List[Tuple[Path, Path]]:
    """
    Finds modified, added, and deleted files in a git repo against HEAD,
    extracts the HEAD version into temp_dir for the left side,
    and pairs it with the working tree file for the right side.
    """
    pairs: List[Tuple[Path, Path]] = []
    if not repo_path.exists():
        return pairs

    for pkg in packages:
        pkg_dir = repo_path / pkg
        if not pkg_dir.exists():
            continue
        cmd = [
            "git", "-C", str(repo_path), "diff", "--name-status", "HEAD", "--",
            f"{pkg}/",
        ]
        # check=False is intentional: git diff returns exit code 1 when differences exist.
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode not in (0, 1):
            logger.warning(f"git diff failed for package '{pkg}' with exit code {res.returncode}: {res.stderr.strip()}")
            continue
        for line in res.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            status, rel_path_str = parts[0], parts[1]
            rel_path = Path(rel_path_str)
            working_file = repo_path / rel_path
            if not check_is_trackable(working_file):
                continue

            if status.startswith("A"):
                empty_left = ensure_empty_file(temp_dir / "empty" / rel_path)
                pairs.append((empty_left, working_file))
            else:
                head_target = temp_dir / "head" / rel_path
                head_target.parent.mkdir(parents=True, exist_ok=True)

                # check=False is intentional: new/uncommitted files are absent from HEAD, handled via fallback.
                show_res = subprocess.run(
                    ["git", "-C", str(repo_path), "show", f"HEAD:{rel_path_str}"],
                    capture_output=True,
                    check=False,
                )
                if show_res.returncode == 0:
                    head_target.write_bytes(show_res.stdout)
                else:
                    head_target.touch()

                if status.startswith("D"):
                    empty_right = ensure_empty_file(temp_dir / "empty" / rel_path)
                    pairs.append((head_target, empty_right))
                else:
                    pairs.append((head_target, working_file))

    return pairs


def collect_pending_delta_pairs(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    temp_dir: Path,
) -> List[Tuple[Path, Path]]:
    """
    Collects file pairs between install/ (left/deployed) and render/ (right/candidate).
    Calls compare_folders instead of 'git diff --no-index' to avoid content-based diffing and focus on file presence and structure.
    NEW packages (render only) appear as all-added pairs; ORPHAN packages (install only) as all-deleted pairs.
    """
    from ..utils.file_inspect import tree_files
    to_diff, new_pkgs, orphan_pkgs = get_pending_delta_worklist(workspace_config, packages)
    pairs: List[Tuple[Path, Path]] = []

    for pkg in new_pkgs:
        render_pkg = workspace_config.render_path / pkg
        for rel_f in (f for f in tree_files(render_pkg) if check_is_trackable(render_pkg / f)):
            empty_left = ensure_empty_file(temp_dir / "empty" / pkg / rel_f)
            pairs.append((empty_left, render_pkg / rel_f))

    for pkg in orphan_pkgs:
        install_pkg = workspace_config.install_path / pkg
        for rel_f in (f for f in tree_files(install_pkg) if check_is_trackable(install_pkg / f)):
            empty_right = ensure_empty_file(temp_dir / "empty" / pkg / rel_f)
            pairs.append((install_pkg / rel_f, empty_right))

    for pkg, _, _ in to_diff:
        install_pkg = workspace_config.install_path / pkg
        render_pkg = workspace_config.render_path / pkg

        diff = compare_folders(render_pkg, install_pkg, resolve_symlinks=False, dir_mode=DirMode.NO_DIR)
        for rel_f in (f for f in diff.modified if check_is_trackable(render_pkg / f) and check_is_trackable(install_pkg / f)):
            pairs.append((install_pkg / rel_f, render_pkg / rel_f))
        for rel_f in (f for f in diff.added if check_is_trackable(render_pkg / f)):
            empty_left = ensure_empty_file(temp_dir / "empty" / pkg / rel_f)
            pairs.append((empty_left, render_pkg / rel_f))
        for rel_f in (f for f in diff.deleted if check_is_trackable(install_pkg / f)):
            empty_right = ensure_empty_file(temp_dir / "empty" / pkg / rel_f)
            pairs.append((install_pkg / rel_f, empty_right))

    return pairs


# =====================================================================
# Layer 3: Terminal Git Diff Runners & Structured Diff Collectors
# =====================================================================

def collect_git_repo_diff_details(
    repo_path: Path,
    pkg: str,
) -> List[FileDiffDetail]:
    """Collects FileDiffDetail list for a package in a git repository against HEAD."""
    diff = parse_git_status_porcelain(repo_path, pkg)
    return [
        *(FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="added") for p in diff.added),
        *(FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="modified") for p in diff.modified),
        *(FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="deleted") for p in diff.deleted),
        *(
            FileDiffDetail(
                path=(Path(pkg) / r.new_path).as_posix(),
                change_type="renamed",
                renamed_from=(Path(pkg) / r.old_path).as_posix(),
            )
            for r in diff.renamed
        ),
    ]


def collect_pending_folder_diff_details(
    workspace_config: WorkspaceConfig,
    pkg: str,
) -> List[FileDiffDetail]:
    """Collects FileDiffDetail list between install/ and render/ for a package."""
    render_pkg = workspace_config.render_path / pkg
    install_pkg = workspace_config.install_path / pkg

    if render_pkg.exists() and install_pkg.exists():
        diff = compare_folders(render_pkg, install_pkg, dir_mode=DirMode.NO_DIR)
        return [
            *(
                FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="added")
                for p in diff.added
                if check_is_trackable(render_pkg / p)
            ),
            *(
                FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="modified")
                for p in diff.modified
                if check_is_trackable(render_pkg / p) and check_is_trackable(install_pkg / p)
            ),
            *(
                FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="deleted")
                for p in diff.deleted
                if check_is_trackable(install_pkg / p)
            ),
        ]
    elif render_pkg.exists() and not install_pkg.exists():
        from ..utils.file_inspect import tree_files
        return [
            FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="added")
            for p in tree_files(render_pkg)
            if check_is_trackable(render_pkg / p)
        ]
    elif not render_pkg.exists() and install_pkg.exists():
        from ..utils.file_inspect import tree_files
        return [
            FileDiffDetail(path=(Path(pkg) / p).as_posix(), change_type="deleted")
            for p in tree_files(install_pkg)
            if check_is_trackable(install_pkg / p)
        ]

    return []


# Strips ANSI SGR color/style escape sequences (e.g., `\x1b[1m`, `\x1b[32m`, `\x1b[m`)
# emitted when git diff is invoked with `--color=always`.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Zero-width multiline lookahead splitting full `git diff` output at the start of each
# per-file diff block (`diff --git ...` or `diff --no-index ...`), even when preceded by
# leading ANSI color codes (e.g., `\x1b[1mdiff --git ...`).
# Note: In Python 3.7+, a zero-width match at index 0 produces a leading `""` element,
# so any patch output with >= 1 file block splits into `len(chunks) >= 2`.
DIFF_SPLIT_RE = re.compile(r"(?m)(?=^(?:\x1b\[[0-9;]*m)*diff --(?:git|no-index) )")

# Parses the two compared file paths (`p1` and `p2`) from a `diff --git` or `diff --no-index`
# header line after ANSI codes are stripped. Handles:
#   - Standard repo diff headers with `a/` and `b/` prefixes:
#       `diff --git a/pkg/file.txt b/pkg/file.txt` -> p1="pkg/file.txt", p2="pkg/file.txt"
#   - Directory/file `--no-index` headers without `a/` or `b/` prefixes:
#       `diff --no-index install/pkg/file.txt render/pkg/file.txt`
#   - Quoted paths (emitted by git when paths contain spaces or special characters):
#       `diff --git "a/pkg/my file.txt" "b/pkg/my file.txt"`
DIFF_HEADER_RE = re.compile(r'diff --(?:git|no-index)\s+"?(?:[ab]/)?(?P<p1>[^\n"]+?)"?\s+"?(?:[ab]/)?(?P<p2>[^\n"]+?)"?$')

# Matches a per-file line in `git diff --stat` output (e.g., ` pkg/file.txt | 2 +-`),
# capturing the path expression before the `|` separator as `path`. Summary footer lines
# (e.g., ` 1 file changed, 1 insertion(+)`) do not contain `|` and do not match.
STAT_LINE_RE = re.compile(r"^\s*(?P<path>[^|]+?)\s*\|\s*")

# Matches git's brace-compressed rename/cross-directory notation in `--stat` lines,
# e.g., `{install => render}/pkg/file.txt` or `pkg/{old.txt => new.txt}`.
STAT_BRACE_RE = re.compile(r"\{\s*([^=>]*?)\s*=>\s*([^}]*?)\s*\}")


def extract_paths_from_diff_header(first_line: str) -> Tuple[Optional[str], Optional[str]]:
    """Extracts the two paths from a git diff header line, stripping ANSI escapes and C-quotes."""
    clean_line = ANSI_ESCAPE_RE.sub("", first_line).strip()
    m = DIFF_HEADER_RE.search(clean_line)
    if m:
        p1, p2 = m.group("p1"), m.group("p2")
        if '"' in clean_line:
            p1 = unquote_git_path(f'"{p1}"')
            p2 = unquote_git_path(f'"{p2}"')
        return p1, p2
    return None, None


def is_diff_output_chunk_candidate(chunk: str) -> bool:
    """Returns True if the diff block corresponds to files that are valid trackable candidates."""
    lines = chunk.splitlines()
    if not lines:
        return False
    p1, p2 = extract_paths_from_diff_header(lines[0])
    if p1 and p2:
        if not check_is_trackable(Path(p1)) or not check_is_trackable(Path(p2)):
            return False
    return True


def is_stat_line_candidate(line: str) -> bool:
    """Returns True if a git diff --stat line corresponds to valid trackable files.

    Git formats `--stat` path columns in three ways depending on how the two paths relate:
    1. Single path (`pkg/file.txt`): standard in-place modification in `git diff --stat`.
    2. Brace-compressed arrow (`{install => render}/pkg/file.txt` or `pkg/{a => b}/file.txt`):
       emitted when comparing two paths that share a common directory prefix or suffix
       (such as `git diff --no-index install/pkg render/pkg`). Simply stripping `{}` and
       splitting on `=>` would lose the shared suffix on the left path (`install` instead
       of `install/pkg/file.txt`), so `STAT_BRACE_RE.sub` is used to reconstruct both full paths.
    3. Unbraced arrow (`/dev/null => render/pkg/file.txt` or `tmp_dir => render/pkg/file.txt`):
       emitted when the two paths share neither a common prefix nor a common suffix.
    """
    clean_line = ANSI_ESCAPE_RE.sub("", line)
    m = STAT_LINE_RE.match(clean_line)
    if not m:
        return True
    # Unquote and normalize git C-quoted paths (e.g. `"render\\pkg/.drift/render_lock.json"` on Windows)
    raw_path = unquote_git_path(m.group("path").strip()).replace('"', "").replace("\\", "/")
    if "{" in raw_path and "}" in raw_path and "=>" in raw_path:
        paths: Tuple[str, ...] = (
            STAT_BRACE_RE.sub(r"\1", raw_path),
            STAT_BRACE_RE.sub(r"\2", raw_path),
        )
    elif "=>" in raw_path:
        left, right = raw_path.split("=>", 1)
        paths = (left.strip(), right.strip())
    else:
        paths = (raw_path,)

    return all(
        p == "/dev/null" or check_is_trackable(Path(p))
        for p in paths
    )


def filter_diff_output(diff_text: str) -> str:
    """Filters diff output to exclude temporary files and internal control plane lock files."""
    if not diff_text:
        return diff_text
    chunks = DIFF_SPLIT_RE.split(diff_text)
    # When `diff_text` contains `diff --git` or `diff --no-index` headers, `DIFF_SPLIT_RE`
    # splits before the first header at index 0, yielding `len(chunks) >= 2` (with `chunks[0] == ""`).
    # When `--stat` is used, no `diff --` headers exist and `len(chunks) == 1`.
    if len(chunks) > 1:
        return "".join(filter(is_diff_output_chunk_candidate, chunks))
    lines = diff_text.splitlines(keepends=True)
    return "".join(filter(is_stat_line_candidate, lines))


def run_repo_diff(
    repo_path: Path,
    packages: Sequence[str],
    git_options: Sequence[str],
) -> bool:
    """Helper to run git diff within a specific repository for a set of packages.
    Returns True if any diff output was produced, False otherwise.
    """
    if not repo_path.exists():
        logger.warning(f"Repository directory does not exist: {repo_path}")
        return False

    had_output = False
    for pkg in packages:
        # We use pathspecs after '--' to avoid revision ambiguity
        cmd = [
            "git", "-C", str(repo_path), "diff",
            *git_options,
            "--", f"{pkg}/",
        ]
        # check=False is intentional: git diff returns exit code 1 when differences exist.
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode not in (0, 1):
            logger.warning(f"git diff failed for package '{pkg}' with code {res.returncode}: {res.stderr.strip()}")
        filtered_stdout = filter_diff_output(res.stdout) if res.stdout else ""
        if filtered_stdout:
            sys.stdout.write(filtered_stdout)
            had_output = True
        if res.stderr:
            sys.stderr.write(res.stderr)
    return had_output


def run_pending_delta_diff(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    git_options: Sequence[str],
) -> bool:
    """Helper to run git diff --no-index between render/ and install/ layers.
    NEW packages diff as all-added against an empty directory; ORPHAN packages diff as all-deleted.
    Returns True if any diff output was produced, False otherwise.
    """
    to_diff, new_pkgs, orphan_pkgs = get_pending_delta_worklist(workspace_config, packages)

    had_output = False
    base_cmd = ["git", "diff", "--no-index", *git_options]

    if new_pkgs or orphan_pkgs:
        with tempfile.TemporaryDirectory() as empty_td:
            empty_dir = Path(empty_td)
            for pkg in new_pkgs:
                logger.info(f"✨ Package '{pkg}' is NEW (exists in render but not install).")
                cmd = [*base_cmd, empty_dir.as_posix(), (workspace_config.render_path / pkg).as_posix()]
                # check=False is intentional: git diff returns exit code 1 when differences exist.
                res = subprocess.run(cmd, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")
                if res.returncode not in (0, 1):
                    logger.warning(f"git diff --no-index failed for '{pkg}' with code {res.returncode}: {res.stderr.strip()}")
                filtered_stdout = filter_diff_output(res.stdout) if res.stdout else ""
                if filtered_stdout:
                    sys.stdout.write(filtered_stdout)
                    had_output = True
                if res.stderr:
                    sys.stderr.write(res.stderr)
            for pkg in orphan_pkgs:
                logger.info(f"⚠️  Package '{pkg}' is ORPHAN (exists in install but not render).")
                cmd = [*base_cmd, (workspace_config.install_path / pkg).as_posix(), empty_dir.as_posix()]
                # check=False is intentional: git diff returns exit code 1 when differences exist.
                res = subprocess.run(cmd, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace")
                if res.returncode not in (0, 1):
                    logger.warning(f"git diff --no-index failed for '{pkg}' with code {res.returncode}: {res.stderr.strip()}")
                filtered_stdout = filter_diff_output(res.stdout) if res.stdout else ""
                if filtered_stdout:
                    sys.stdout.write(filtered_stdout)
                    had_output = True
                if res.stderr:
                    sys.stderr.write(res.stderr)

    for _, rel_install, rel_render in to_diff:
        cmd = [*base_cmd, rel_install.as_posix(), rel_render.as_posix()]
        # check=False is intentional: git diff returns exit code 1 when differences exist.
        res = subprocess.run(
            cmd,
            cwd=str(workspace_config.drift_root),
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode not in (0, 1):
            logger.warning(f"git diff --no-index failed with code {res.returncode}: {res.stderr.strip()}")
        filtered_stdout = filter_diff_output(res.stdout) if res.stdout else ""
        if filtered_stdout:
            sys.stdout.write(filtered_stdout)
            had_output = True
        if res.stderr:
            sys.stderr.write(res.stderr)
    return had_output


# =====================================================================
# Layer 4: Diff Strategy Dispatchers
# =====================================================================

def run_side_by_side_diff(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    diff_type: WorkspaceDiffType,
) -> None:
    """Collects file pairs and launches side-by-side visual diff editor."""
    with tempfile.TemporaryDirectory() as td:
        temp_dir = Path(td)
        if diff_type == WorkspaceDiffType.TEMPLATE:
            logger.info("🔍 [Diff A] Visualizing Template Evolution (src/ -> render/)...")
            pairs = collect_repo_diff_pairs(workspace_config.render_path, packages, temp_dir)
        elif diff_type == WorkspaceDiffType.SYSTEM:
            logger.info("🔍 [Diff B] Visualizing System Drift (System -> install/)...")
            pairs = collect_repo_diff_pairs(workspace_config.install_path, packages, temp_dir)
        elif diff_type == WorkspaceDiffType.PENDING:
            logger.info("🔍 [Diff Δ] Visualizing Pending Delta (render/ -> install/)...")
            pairs = collect_pending_delta_pairs(workspace_config, packages, temp_dir)
        else:
            pairs = []
        if not pairs:
            logger.info("✨ No differences detected.")
            return
        launch_side_by_side_editor(pairs)


def run_terminal_diff(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    diff_type: WorkspaceDiffType,
    stat: bool = False,
) -> None:
    """Dispatches standard terminal git diffs for Diff A, B, or Δ."""
    git_options = ["--color=always"]
    if stat:
        git_options.append("--stat")

    had_output = False
    if diff_type == WorkspaceDiffType.TEMPLATE:
        logger.info("🔍 [Diff A] Visualizing Template Evolution (src/ -> render/)...")
        had_output = run_repo_diff(workspace_config.render_path, packages, git_options)

    elif diff_type == WorkspaceDiffType.SYSTEM:
        logger.info("🔍 [Diff B] Visualizing System Drift (System -> install/)...")
        had_output = run_repo_diff(workspace_config.install_path, packages, git_options)

    elif diff_type == WorkspaceDiffType.PENDING:
        logger.info("🔍 [Diff Δ] Visualizing Pending Delta (render/ -> install/)...")
        had_output = run_pending_delta_diff(workspace_config, packages, git_options)

    if not had_output:
        logger.info("✨ No differences detected.")


# =====================================================================
# Layer 5: Public Primitive Entry Point
# =====================================================================

def run_primitive_15_workspace_diff(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    diff_type: WorkspaceDiffType = WorkspaceDiffType.PENDING,
    side_by_side: bool = False,
    stat: bool = False,
    quiet: bool = False
) -> DiffResult:
    """
    Visualizes changes between configuration layers and returns structured DiffResult.
    1. Ensures repositories are up-to-date (Transient Render/Reverse-Sync).
    2. Executes appropriate git diff command or side-by-side editor if not quiet.
    3. Returns DiffResult containing package and file diff details.
    """
    # Identify target packages
    discovered_in_install = workspace_config.get_package_names_from_dir(workspace_config.install_path)
    discovered_in_src = workspace_config.get_package_names_from_source_dir()
    all_discovered = sorted(set(discovered_in_install) | set(discovered_in_src))

    packages = workspace_config.filter_given_packages_by_target(
        available_packages=all_discovered,
        target_packages=package_names or None,
    )
    if not packages and package_names:
        logger.warning(f"No packages found matching: {', '.join(package_names)}")
        return DiffResult(command="diff", diff_type=diff_type, packages=[])

    # Update repositories to reflect latest state
    from .reverse_sync import run_primitive_1_reverse_sync
    from .render_repo import run_primitive_2_render_packages

    if diff_type in (WorkspaceDiffType.SYSTEM, WorkspaceDiffType.PENDING):
        syncable = [pkg for pkg in packages if (workspace_config.install_path / pkg).is_dir()]
        if syncable:
            run_primitive_1_reverse_sync(workspace_config, package_names=syncable)
    if diff_type in (WorkspaceDiffType.TEMPLATE, WorkspaceDiffType.PENDING):
        renderable = [pkg for pkg in packages if (workspace_config.source_path / pkg).is_dir()]
        if renderable:
            run_primitive_2_render_packages(workspace_config, target_pkgs=renderable)

    # Collect structured diff details
    package_details: List[PackageDiffDetail] = []
    for pkg in packages:
        if diff_type == WorkspaceDiffType.TEMPLATE:
            files = collect_git_repo_diff_details(workspace_config.render_path, pkg)
        elif diff_type == WorkspaceDiffType.SYSTEM:
            files = collect_git_repo_diff_details(workspace_config.install_path, pkg)
        elif diff_type == WorkspaceDiffType.PENDING:
            files = collect_pending_folder_diff_details(workspace_config, pkg)
        else:
            files = []
        package_details.append(PackageDiffDetail(package=pkg, has_changes=bool(files), files=files))

    if not quiet:
        if side_by_side:
            run_side_by_side_diff(workspace_config, packages, diff_type)
        else:
            run_terminal_diff(workspace_config, packages, diff_type, stat=stat)

    return DiffResult(command="diff", diff_type=diff_type, packages=package_details)

