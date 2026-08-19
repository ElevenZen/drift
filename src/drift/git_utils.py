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
