"""Git repository utility functions."""

import os
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Union
from dataclasses import dataclass, field

from .process_utils import run_command
from .file_inspect import is_temp_file
from ..core.constants import DRIFT_GENERATED_FILES

logger = logging.getLogger(__name__)


@dataclass
class GitRename:
    """Represents a file rename in git."""
    old_path: Path
    new_path: Path


@dataclass
class GitStatusDiff:
    """Structured representation of git status porcelain changes."""
    added: List[Path] = field(default_factory=list)
    modified: List[Path] = field(default_factory=list)
    deleted: List[Path] = field(default_factory=list)
    renamed: List[GitRename] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted or self.renamed)

    def all_paths(self) -> List[Path]:
        paths = list(self.added) + list(self.modified) + list(self.deleted)
        for r in self.renamed:
            paths.append(r.new_path)
        return paths


def get_git_status_porcelain(
    repo_path: Path,
    pkg_path: Optional[Union[str, Path]] = None,
) -> List[str]:
    """Returns the output lines of git status --porcelain for a given repository and package/sub path."""
    if not repo_path.exists():
        return []
    cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
    if pkg_path:
        cmd.append(str(pkg_path))
    res = run_command(cmd, text=True, check=False, suppress_output=True)
    if res.returncode != 0 or not res.stdout or not res.stdout.strip():
        return []
    return res.stdout.splitlines()


def has_uncommitted_modifications(
    repo_path: Path,
    sub_path: Optional[Union[Path, str]] = None,
) -> bool:
    """Checks if a git repository (or a specific path inside it) has uncommitted local modifications.

    Uncommitted modifications include staged changes, unstaged changes, and untracked files.
    """
    return bool(get_git_status_porcelain(repo_path, sub_path))


def _normalize_pkg_relative_path(path: Path, pkg_name: Optional[str]) -> Path:
    """Trims leading pkg_name directory prefix from path if present."""
    if pkg_name and path.parts and path.parts[0] == pkg_name:
        return path.relative_to(Path(pkg_name))
    return path


def _parse_rename_entry(
    path_str: str,
    pkg_name: Optional[str],
    ignored_files: Sequence[str],
) -> Optional[GitRename]:
    """Parses a git rename status string ('old -> new') into a GitRename object."""
    old_str, new_str = path_str.split(" -> ", 1)
    old_p = Path(old_str.strip('" '))
    new_p = Path(new_str.strip('" '))
    if new_p.name in ignored_files or is_temp_file(new_p):
        return None
    return GitRename(
        old_path=_normalize_pkg_relative_path(old_p, pkg_name),
        new_path=_normalize_pkg_relative_path(new_p, pkg_name),
    )


def parse_git_status_porcelain(
    repo_path: Path,
    pkg_name: Optional[str] = None,
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
) -> GitStatusDiff:
    """Parses git status --porcelain for a repository (scoped to pkg_name) into GitStatusDiff."""
    lines = get_git_status_porcelain(repo_path, f"{pkg_name}/" if pkg_name else None)
    if not lines:
        return GitStatusDiff()

    added: List[Path] = []
    modified: List[Path] = []
    deleted: List[Path] = []
    renamed: List[GitRename] = []

    for line in lines:
        if len(line) < 4:
            continue
        index_stat = line[0]
        work_stat = line[1]
        path_str = line[3:].strip()

        if " -> " in path_str:
            rename_entry = _parse_rename_entry(path_str, pkg_name, ignored_files)
            if rename_entry:
                renamed.append(rename_entry)
            continue

        p = Path(path_str.strip('" '))
        if p.name in ignored_files or is_temp_file(p):
            continue

        rel_p = _normalize_pkg_relative_path(p, pkg_name)

        if index_stat == "?" or work_stat == "?" or index_stat == "A" or work_stat == "A":
            added.append(rel_p)
        elif index_stat == "D" or work_stat == "D":
            deleted.append(rel_p)
        elif index_stat in ("M", "R") or work_stat in ("M", "R"):
            modified.append(rel_p)

    return GitStatusDiff(added=added, modified=modified, deleted=deleted, renamed=renamed)


def _is_pkg_stageable(repo_path: Path, pkg: str) -> bool:
    """Checks if a package folder exists on disk or has files tracked in git."""
    if (repo_path / pkg).exists():
        return True
    res = run_command(
        ["git", "-C", str(repo_path), "ls-files", f"{pkg}/"],
        text=True,
        check=False,
        suppress_output=True,
    )
    return bool(res.stdout and res.stdout.strip())


def _resolve_pkg_stage_targets(repo_path: Path, target_pkgs: Sequence[str]) -> List[str]:
    """Filters target packages to those that exist on disk or are tracked in git."""
    return [f"{pkg}/" for pkg in target_pkgs if _is_pkg_stageable(repo_path, pkg)]


def commit_repo_changes(
    repo_path: Path,
    commit_message: str,
    target_pkgs: Sequence[str] = (),
    repo_name: str = "repository",
) -> bool:
    """
    Stages and commits changes in a git repository.
    Handles pathspec existence and git tracking for scoped commits.
    Returns True if a commit was made, False otherwise.
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"{repo_name} directory does not exist: {repo_path}")

    # 1. Stage changes (scoped to package folders if provided, otherwise all changes)
    if target_pkgs:
        stage_targets = _resolve_pkg_stage_targets(repo_path, target_pkgs)
        if (repo_path / "state.toml").exists():
            stage_targets.append("state.toml")

        if not stage_targets:
            # Nothing to add for these specific packages
            return False

        add_cmd = ["git", "-C", str(repo_path), "add", *stage_targets]
    else:
        add_cmd = ["git", "-C", str(repo_path), "add", "-A"]

    try:
        run_command(add_cmd, text=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to stage changes in {repo_name}. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to stage changes in {repo_name}: {e.stderr}") from e

    # 2. Check if there are uncommitted modifications to commit
    if target_pkgs:
        has_changes = any(has_uncommitted_modifications(repo_path, f"{pkg}/") for pkg in target_pkgs)
    else:
        has_changes = has_uncommitted_modifications(repo_path)

    if not has_changes:
        return False

    # 3. Perform git commit with the given commit message
    try:
        run_command(
            ["git", "-C", str(repo_path), "commit", "-m", commit_message],
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to commit changes in {repo_name}. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to commit changes in {repo_name}: {e.stderr}") from e


def is_git_tracked(dir_path: Path) -> bool:
    """Checks if a directory is inside a Git repository."""
    res = run_command(
        ["git", "-C", str(dir_path), "rev-parse", "--git-dir"],
        text=True,
        check=False,
        suppress_output=True,
    )
    return res.returncode == 0


def get_drift_root(dir_path: Path, force: bool = False) -> Path:
    """Resolves the root of the drift workspace (git toplevel)."""
    try:
        res = run_command(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(dir_path),
            text=True,
        )
        return Path(res.stdout.strip()).resolve()
    except subprocess.CalledProcessError as e:
        ensure_git_repository_health(dir_path, force=force)
        # Check if the error is due to not being inside a Git repo
        if not is_git_tracked(dir_path):
            raise RuntimeError(
                f"The directory '{dir_path}' is not inside a Git repository. "
                "drift requires a Git-backed workspace to manage configuration state. "
                "Run 'drift init' to initialize a new workspace, or specify '--no-git-root' to run drift in literal mode."
            )
        raise RuntimeError(f"Failed to resolve git repository root: {e.stderr.strip()}")


def is_bare_repository(dir_path: Path) -> bool:
    """Checks if the Git repository is a bare repository."""
    res = run_command(
        ["git", "-C", str(dir_path), "rev-parse", "--is-bare-repository"],
        text=True,
        check=False,
        suppress_output=True,
    )
    return res.returncode == 0 and res.stdout.strip() == "true"


def is_detached_head(dir_path: Path) -> bool:
    """Checks if the Git repository is in a detached HEAD state."""
    res = run_command(
        ["git", "-C", str(dir_path), "symbolic-ref", "-q", "HEAD"],
        text=True,
        check=False,
        suppress_output=True,
    )
    return res.returncode != 0


def is_merge_or_rebase_in_progress(dir_path: Path) -> bool:
    """Checks if a merge or rebase operation is currently in progress."""
    res = run_command(
        ["git", "-C", str(dir_path), "rev-parse", "--git-dir"],
        text=True,
        check=False,
        suppress_output=True,
    )
    if res.returncode != 0 or not res.stdout:
        return False
    git_dir = (dir_path / res.stdout.strip()).resolve()

    return (
        (git_dir / "MERGE_HEAD").exists()
        or (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists()
    )


def ensure_git_repository_health(dir_path: Path, force: bool = False) -> None:
    """Validates that the Git repository at dir_path is healthy and compatible with drift."""
    if force or not is_git_tracked(dir_path):
        return
    if is_bare_repository(dir_path):
        raise RuntimeError("Bare Git repositories are not supported for drift workspace.")
    if is_detached_head(dir_path):
        raise RuntimeError("Git repository is in a detached HEAD state.")
    if is_merge_or_rebase_in_progress(dir_path):
        raise RuntimeError("Git repository is currently in the middle of a merge or rebase operation.")


def check_repo_can_commit(repo_path: Path) -> None:
    """Verifies that Git user.name and user.email are configured for the repository.
    Raises a RuntimeError if either configuration is missing, preventing commit failures.
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"Directory does not exist: {repo_path}")

    # Query user.name
    has_env_name = bool(os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GIT_COMMITTER_NAME"))
    if not has_env_name:
        res = run_command(
            ["git", "-C", str(repo_path), "config", "user.name"],
            text=True,
            check=False,
            suppress_output=True,
        )
        if res.returncode != 0 or not res.stdout or not res.stdout.strip():
            raise RuntimeError(
                f"Git configuration error: 'user.name' is not configured in the repository or globally for '{repo_path}'. "
                "Please run: git config --global user.name \"Your Name\""
            )

    # Query user.email
    has_env_email = bool(os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("GIT_COMMITTER_EMAIL"))
    if not has_env_email:
        res = run_command(
            ["git", "-C", str(repo_path), "config", "user.email"],
            text=True,
            check=False,
            suppress_output=True,
        )
        if res.returncode != 0 or not res.stdout or not res.stdout.strip():
            raise RuntimeError(
                f"Git configuration error: 'user.email' is not configured in the repository or globally for '{repo_path}'. "
                "Please run: git config --global user.email \"you@example.com\""
            )


def git_init_repo(dir_path: Path, name: str) -> bool:
    """Initializes a git repository at dir_path.

    Raises RuntimeError if initialization fails, returns True on success.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        run_command(
            ["git", "init"],
            cwd=str(dir_path),
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to initialize {name} git repository: {e.stderr}")


def append_to_gitignore(drift_root: Path, folders_to_ignore: Sequence[str]) -> None:
    """Appends folders to .gitignore if they are not already ignored."""
    gitignore_path = drift_root / ".gitignore"
    existing_content = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    lines = existing_content.splitlines()
    normalized_lines = {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}

    new_ignores = [
        folder for folder in folders_to_ignore
        if folder not in normalized_lines and folder.rstrip("/") not in normalized_lines
    ]

    if new_ignores:
        with gitignore_path.open("a", encoding="utf-8") as f:
            if existing_content and not existing_content.endswith("\n"):
                f.write("\n")
            f.write("# drift workspace\n")
            for folder in new_ignores:
                f.write(f"{folder}\n")
