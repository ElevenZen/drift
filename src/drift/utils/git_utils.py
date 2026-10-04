"""Git repository utility functions."""

import os
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Union
from dataclasses import dataclass, field

from .process_utils import run_command
from .file_inspect import is_temp_file, is_diff_candidate
from .path_utils import to_relative_path
from ..core.constants import DEFAULT_DIFF_EXCLUDE_PATTERNS
from ..core.exceptions import mark_logged

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
    exclude_patterns: Sequence[str] = DEFAULT_DIFF_EXCLUDE_PATTERNS,
) -> List[str]:
    """Returns the output lines of git status --porcelain for a given repository and package/sub path."""
    if not repo_path.exists():
        return []
    cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
    if pkg_path:
        cmd.extend(["--", str(pkg_path), *exclude_patterns])
    elif exclude_patterns:
        cmd.extend(["--", *exclude_patterns])
    # check=False is intentional: non-zero returncode indicates error/clean/untracked, handled explicitly below.
    res = run_command(cmd, text=True, check=False, suppress_output=True)
    if res.returncode != 0 or not res.stdout or not res.stdout.strip():
        return []
    return res.stdout.splitlines()


def has_uncommitted_modifications(
    repo_path: Path,
    sub_path: Optional[Union[Path, str]] = None,
    exclude_patterns: Sequence[str] = DEFAULT_DIFF_EXCLUDE_PATTERNS,
) -> bool:
    """Checks if a git repository (or a specific path inside it) has uncommitted local modifications.

    Uncommitted modifications include staged changes, unstaged changes, and untracked files.
    """
    return bool(get_git_status_porcelain(repo_path, sub_path, exclude_patterns=exclude_patterns))


def _normalize_pkg_relative_path(path: Path, pkg_name: Optional[str]) -> Path:
    """Trims leading pkg_name directory prefix from path if present."""
    return to_relative_path(path, Path(pkg_name)) if pkg_name else path


def _parse_rename_entry(
    path_str: str,
    pkg_name: Optional[str] = None,
) -> Optional[GitRename]:
    """Parses a git rename status string ('old -> new') into a GitRename object."""
    old_str, new_str = path_str.split(" -> ", 1)
    old_p = Path(old_str.strip('" '))
    new_p = Path(new_str.strip('" '))
    if not is_diff_candidate(new_p):
        return None
    return GitRename(
        old_path=_normalize_pkg_relative_path(old_p, pkg_name),
        new_path=_normalize_pkg_relative_path(new_p, pkg_name),
    )


def parse_git_status_porcelain(
    repo_path: Path,
    pkg_name: Optional[str] = None,
    exclude_patterns: Sequence[str] = DEFAULT_DIFF_EXCLUDE_PATTERNS,
) -> GitStatusDiff:
    """Parses git status --porcelain for a repository (scoped to pkg_name) into GitStatusDiff."""
    lines = get_git_status_porcelain(
        repo_path,
        f"{pkg_name}/" if pkg_name else None,
        exclude_patterns=exclude_patterns,
    )
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
            rename_entry = _parse_rename_entry(path_str, pkg_name)
            if rename_entry:
                renamed.append(rename_entry)
            continue

        p = Path(path_str.strip('" '))
        if not is_diff_candidate(p):
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
    if (repo_path / pkg).is_dir():
        return True
    # check=False is intentional: exit code indicates untracked folder/missing files.
    res = run_command(
        ["git", "-C", str(repo_path), "ls-files", f"{pkg}/"],
        text=True,
        check=False,
        suppress_output=True,
    )
    return bool(res.returncode == 0 and res.stdout and res.stdout.strip())


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
        raise mark_logged(RuntimeError(f"Failed to stage changes in {repo_name}: {e.stderr}")) from e

    # 2. Check if there are uncommitted modifications to commit
    if target_pkgs:
        has_changes = any(
            has_uncommitted_modifications(repo_path, f"{pkg}/") for pkg in target_pkgs
        ) or (
            (repo_path / "state.toml").exists()
            and has_uncommitted_modifications(repo_path, "state.toml")
        )
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
        raise mark_logged(RuntimeError(f"Failed to commit changes in {repo_name}: {e.stderr}")) from e


def commit_staged_repo_changes(
    repo_path: Path,
    commit_message: str,
    repo_name: str = "repository",
) -> bool:
    """Commits already-staged changes in a git repository.

    Verifies that changes are present in the staged index ('git diff --cached --quiet')
    before executing git commit. Never modifies working tree or unstaged files.
    Returns True if a commit was made, False otherwise.
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"{repo_name} directory does not exist: {repo_path}")

    res = run_command(
        ["git", "-C", str(repo_path), "diff", "--cached", "--quiet"],
        text=True,
        check=False,
    )
    if res.returncode == 0:
        return False

    try:
        run_command(
            ["git", "-C", str(repo_path), "commit", "-m", commit_message],
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to commit staged changes in {repo_name}. Stderr: {e.stderr}")
        raise mark_logged(RuntimeError(f"Failed to commit staged changes in {repo_name}: {e.stderr}")) from e


def is_git_tracked(dir_path: Path) -> bool:
    """Checks if a directory is inside a Git repository."""
    # check=False is intentional: returncode == 0 directly determines git repository presence.
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
        if not force:
            assert_git_repository_health(dir_path)
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
    # check=False is intentional: inspect returncode and stdout to identify bare repo state.
    res = run_command(
        ["git", "-C", str(dir_path), "rev-parse", "--is-bare-repository"],
        text=True,
        check=False,
        suppress_output=True,
    )
    return res.returncode == 0 and res.stdout.strip() == "true"


def is_detached_head(dir_path: Path) -> bool:
    """Checks if the Git repository is in a detached HEAD state."""
    # check=False is intentional: symbolic-ref exits with non-zero when HEAD is detached.
    res = run_command(
        ["git", "-C", str(dir_path), "symbolic-ref", "-q", "HEAD"],
        text=True,
        check=False,
        suppress_output=True,
    )
    return res.returncode != 0


def is_merge_or_rebase_in_progress(dir_path: Path) -> bool:
    """Checks if a merge or rebase operation is currently in progress."""
    # check=False is intentional: non-zero returncode indicates git-dir resolution failed.
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


def assert_git_repository_health(dir_path: Path) -> None:
    """Validates that the Git repository at dir_path is healthy and compatible with drift."""
    if not is_git_tracked(dir_path):
        return
    if is_bare_repository(dir_path):
        raise RuntimeError("Bare Git repositories are not supported for drift workspace.")
    if is_detached_head(dir_path):
        raise RuntimeError("Git repository is in a detached HEAD state.")
    if is_merge_or_rebase_in_progress(dir_path):
        raise RuntimeError("Git repository is currently in the middle of a merge or rebase operation.")


def configure_repo_git_user(
    repo_path: Path,
    user_name: Optional[str] = None,
    user_email: Optional[str] = None,
) -> None:
    """Configures git user.name and/or user.email locally in a repository.

    Applies 'git config --local user.name/email' so all subsequent git operations
    in that repository use the specified identity without requiring global git config.
    Skips silently when both values are None.
    """
    if user_name:
        run_command(
            ["git", "-C", str(repo_path), "config", "user.name", user_name],
            text=True,
        )
        logger.debug(f"Configured git user.name = '{user_name}' in {repo_path}")
    if user_email:
        run_command(
            ["git", "-C", str(repo_path), "config", "user.email", user_email],
            text=True,
        )
        logger.debug(f"Configured git user.email = '{user_email}' in {repo_path}")


def check_repo_can_commit(repo_path: Path) -> Optional[str]:
    """Checks if Git user.name and user.email are configured for the repository.

    Returns None if both are configured, or an error message string describing
    the missing configuration. Does not raise exceptions or modify state.
    """
    if not repo_path.exists():
        return f"Directory does not exist: {repo_path}"

    # Check user.name
    has_env_name = bool(os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GIT_COMMITTER_NAME"))
    if not has_env_name:
        # check=False is intentional: missing git config returns exit code 1.
        res = run_command(
            ["git", "-C", str(repo_path), "config", "user.name"],
            text=True,
            check=False,
            suppress_output=True,
        )
        if res.returncode != 0 or not res.stdout or not res.stdout.strip():
            return (
                f"Git configuration error: 'user.name' is not configured in the repository or globally for '{repo_path}'. "
                "Please run: git config --global user.name \"Your Name\"\n"
                "Alternatively, set 'git_user_name' under [settings] in config/drift_workspace.toml "
                "and run 'drift repair' to apply it to render/ and install/ repositories."
            )

    # Check user.email
    has_env_email = bool(os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("GIT_COMMITTER_EMAIL"))
    if not has_env_email:
        # check=False is intentional: missing git config returns exit code 1.
        res = run_command(
            ["git", "-C", str(repo_path), "config", "user.email"],
            text=True,
            check=False,
            suppress_output=True,
        )
        if res.returncode != 0 or not res.stdout or not res.stdout.strip():
            return (
                f"Git configuration error: 'user.email' is not configured in the repository or globally for '{repo_path}'. "
                "Please run: git config --global user.email \"you@example.com\"\n"
                "Alternatively, set 'git_user_email' under [settings] in config/drift_workspace.toml "
                "and run 'drift repair' to apply it to render/ and install/ repositories."
            )

    return None


def assert_repo_can_commit(repo_path: Path) -> None:
    """Verifies that Git user.name and user.email are configured for the repository.
    Raises a RuntimeError if either configuration is missing, preventing commit failures.
    """
    error = check_repo_can_commit(repo_path)
    if error:
        raise RuntimeError(error)


def check_repo_git_user_synced(
    repo_path: Path,
    expected_name: Optional[str] = None,
    expected_email: Optional[str] = None,
) -> Optional[str]:
    """Checks whether local git config user.name and user.email match expected workspace settings.

    Returns an error message if out of sync, or None if synchronized (or when expected values are None).
    """
    if expected_name:
        # check=False is intentional: unconfigured key returns exit code 1.
        res = run_command(
            ["git", "-C", str(repo_path), "config", "--local", "user.name"],
            text=True,
            check=False,
            suppress_output=True,
        )
        if res.returncode != 0 or res.stdout.strip() != expected_name:
            return f"Git user.name does not match workspace settings ('{expected_name}')"

    if expected_email:
        # check=False is intentional: unconfigured key returns exit code 1.
        res = run_command(
            ["git", "-C", str(repo_path), "config", "--local", "user.email"],
            text=True,
            check=False,
            suppress_output=True,
        )
        if res.returncode != 0 or res.stdout.strip() != expected_email:
            return f"Git user.email does not match workspace settings ('{expected_email}')"

    return None


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
