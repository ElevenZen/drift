"""Repository validation and health checks using pathlib."""

import os
import subprocess
from pathlib import Path
from typing import Optional


def is_git_tracked(dir_path: Path) -> bool:
    """Checks if a directory is inside a Git repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(dir_path),
            capture_output=True,
            text=True
        )
        return res.returncode == 0
    except Exception:
        return False


def get_drift_root(dir_path: Path, force: bool = False) -> Path:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(dir_path),
            capture_output=True,
            text=True,
            check=True
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
        res = subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=str(dir_path),
            capture_output=True,
            text=True
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except Exception:
        return False


def is_detached_head(dir_path: Path) -> bool:
    """Checks if the Git repository is in a detached HEAD state."""
    try:
        res = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=str(dir_path),
            capture_output=True,
            text=True
        )
        return res.returncode != 0
    except Exception:
        return False


def is_merge_or_rebase_in_progress(dir_path: Path) -> bool:
    """Checks if a merge or rebase operation is currently in progress."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(dir_path),
            capture_output=True,
            text=True,
            check=True
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


def ensure_writable(path: Path) -> None:
    """Ensures that the provided path is valid and read-writable."""
    curr = path.resolve()
    while curr:
        if curr.exists():
            if curr.is_dir() and os.access(curr, os.W_OK | os.X_OK):
                return
            else:
                raise PermissionError(f"Path '{path}' (resolved at '{curr}') is not writable.")
        parent = curr.parent
        if parent == curr:  # Root reached
            break
        curr = parent
    raise ValueError(f"Path '{path}' is invalid.")


def check_existing_workspace_status(drift_root: Path) -> bool:
    """Checks if a complete and valid drift workspace already exists.

    Returns:
        True if a valid and healthy drift workspace is fully initialized.
        False if any part is missing, invalid, or corrupt.
    """
    config_file = drift_root / "config" / "drift.toml"
    if not config_file.exists():
        return False

    # Validate drift.toml syntax & load it
    from .toml_parser import parse_toml
    from .workspace_config import load_workspace_config
    try:
        content = config_file.read_text(encoding="utf-8")
        data = parse_toml(content)
        if "workspace" not in data or "packages" not in data:
            return False
        # Validate workspace_config validation
        load_workspace_config(config_file)
    except Exception:
        return False

    # Check install/state.toml exists and has valid TOML syntax
    state_file = drift_root / "install" / "state.toml"
    if not state_file.exists():
        return False
    try:
        state_content = state_file.read_text(encoding="utf-8")
        parse_toml(state_content)
    except Exception:
        return False

    # Check render/ and install/ directories exist and are healthy git repos
    render_dir = drift_root / "render"
    install_dir = drift_root / "install"
    if not render_dir.is_dir() or not install_dir.is_dir():
        return False

    if not is_git_tracked(render_dir) or not is_git_tracked(install_dir):
        return False

    if is_bare_repository(render_dir) or is_bare_repository(install_dir):
        return False

    return True


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
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        return bool(res.stdout.strip())
    except subprocess.CalledProcessError:
        return False
