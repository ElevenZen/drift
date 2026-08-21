"""Git repository utility functions."""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional
from .file_utils import run_command

logger = logging.getLogger(__name__)

def commit_repo_changes(
    repo_path: Path,
    commit_message: str,
    target_pkgs: Optional[List[str]] = None,
    repo_name: str = "repository"
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
        add_cmd = ["git", "-C", str(repo_path), "add"]
        added_any = False
        for pkg in target_pkgs:
            # Only add pathspec if it exists on disk or is already in the index
            pkg_path = repo_path / pkg
            if pkg_path.exists():
                add_cmd.append(f"{pkg}/")
                added_any = True
            else:
                # Check if it was tracked by git (to stage deletion)
                ls_cmd = ["git", "-C", str(repo_path), "ls-files", f"{pkg}/"]
                try:
                    res = run_command(ls_cmd, capture_output=True, text=True)
                    if res.stdout.strip():
                        add_cmd.append(f"{pkg}/")
                        added_any = True
                except Exception:
                    pass
        if (repo_path / "state.toml").exists():
            add_cmd.append("state.toml")
            added_any = True

        if not added_any:
            # Nothing to add for these specific packages
            return False
    else:
        add_cmd = ["git", "-C", str(repo_path), "add", "-A"]

    try:
        run_command(
            add_cmd,
            text=True
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to stage changes in {repo_name}. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to stage changes in {repo_name}: {e.stderr}") from e

    # 2. Check if there are staged changes to commit (scoped to package folders if provided)
    status_cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
    if target_pkgs:
        for pkg in target_pkgs:
            status_cmd.append(f"{pkg}/")

    try:
        status_res = run_command(
            status_cmd,
            text=True
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to check git status in {repo_name}. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to check git status in {repo_name}: {e.stderr}") from e

    if not status_res.stdout.strip():
        return False

    # 3. Perform git commit with the given commit message
    try:
        run_command(
            ["git", "-C", str(repo_path), "commit", "-m", commit_message],
            text=True
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to commit changes in {repo_name}. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to commit changes in {repo_name}: {e.stderr}") from e


def is_git_tracked(dir_path: Path) -> bool:
    """Checks if a directory is inside a Git repository."""
    try:
        res = run_command(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(dir_path),
            check=False,
            text=True
        )
        return res.returncode == 0
    except Exception:
        return False


def get_drift_root(dir_path: Path, force: bool = False) -> Path:
    """Resolves the root of the drift workspace (git toplevel)."""
    try:
        res = run_command(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(dir_path),
            text=True
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
    try:
        res = run_command(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=str(dir_path),
            text=True
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except Exception:
        return False


def is_detached_head(dir_path: Path) -> bool:
    """Checks if the Git repository is in a detached HEAD state."""
    try:
        res = run_command(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=str(dir_path),
            check=False,
            text=True
        )
        return res.returncode != 0
    except Exception:
        return False


def is_merge_or_rebase_in_progress(dir_path: Path) -> bool:
    """Checks if a merge or rebase operation is currently in progress."""
    try:
        res = run_command(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(dir_path),
            text=True
        )
        git_dir = (dir_path / res.stdout.strip()).resolve()
    except Exception:
        return False

    # Check for merge
    merge_head = git_dir / "MERGE_HEAD"
    if merge_head.exists():
        return True

    # Check for rebase
    rebase_merge = git_dir / "rebase-merge"
    rebase_apply = git_dir / "rebase-apply"
    if rebase_merge.exists() or rebase_apply.exists():
        return True

    return False


def ensure_git_repository_health(dir_path: Path, force: bool = False) -> None:
    """Validates that the Git repository at dir_path is healthy and compatible with drift."""
    if force:
        return
    if not is_git_tracked(dir_path):
        return
    if is_bare_repository(dir_path):
        raise RuntimeError("Bare Git repositories are not supported for drift workspace.")
    if is_detached_head(dir_path):
        raise RuntimeError("Git repository is in a detached HEAD state.")
    if is_merge_or_rebase_in_progress(dir_path):
        raise RuntimeError("Git repository is currently in the middle of a merge or rebase operation.")


def has_uncommitted_modifications(repo_path: Path, sub_path: Optional[Path] = None) -> bool:
    """Checks if a git repository (or a specific path inside it) has uncommitted local modifications.

    Uncommitted modifications include staged changes, unstaged changes, and untracked files.
    """
    if not is_git_tracked(repo_path):
        return False

    cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
    if sub_path:
        cmd.append(str(sub_path))

    try:
        res = run_command(
            cmd,
            text=True
        )
        return bool(res.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def get_git_status_porcelain(repo_path: Path, pkg_path: Optional[str] = None) -> List[str]:
    """Returns the output of git status --porcelain for a given repository and package path."""
    if not repo_path.exists():
        return []
    cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
    if pkg_path:
        cmd.append(pkg_path)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.splitlines()
    except subprocess.CalledProcessError:
        return []


def check_repo_can_commit(repo_path: Path) -> None:
    """Verifies that Git user.name and user.email are configured for the repository.
    Raises a RuntimeError if either configuration is missing, preventing commit failures.
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"Directory does not exist: {repo_path}")

    # Query user.name
    try:
        subprocess.run(["git", "-C", str(repo_path), "config", "user.name"], capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Git configuration error: 'user.name' is not configured in the repository or globally for '{repo_path}'. "
            "Please run: git config --global user.name \"Your Name\""
        ) from e

    # Query user.email
    try:
        subprocess.run(["git", "-C", str(repo_path), "config", "user.email"], capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Git configuration error: 'user.email' is not configured in the repository or globally for '{repo_path}'. "
            "Please run: git config --global user.email \"you@example.com\""
        ) from e

