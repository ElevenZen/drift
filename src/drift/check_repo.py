import os
import subprocess

def is_git_tracked(dir_path: str) -> bool:
    """Checks if a directory is inside a Git repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=dir_path,
            capture_output=True,
            text=True
        )
        return res.returncode == 0
    except Exception:
        return False


def get_drift_root(dir_path: str, force: bool = False) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=True
        )
        return os.path.abspath(res.stdout.strip())
    except subprocess.CalledProcessError as e:
        ensure_git_repository_health(dir_path, force=force)
        raise RuntimeError(f"Failed to resolve git repository root: {e.stderr}")


def is_bare_repository(dir_path: str) -> bool:
    """Checks if the Git repository is a bare repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=dir_path,
            capture_output=True,
            text=True
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except Exception:
        return False


def is_detached_head(dir_path: str) -> bool:
    """Checks if the Git repository is in a detached HEAD state."""
    try:
        res = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=dir_path,
            capture_output=True,
            text=True
        )
        return res.returncode != 0
    except Exception:
        return False


def is_merge_or_rebase_in_progress(dir_path: str) -> bool:
    """Checks if a merge or rebase operation is currently in progress."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=True
        )
        git_dir = os.path.abspath(os.path.join(dir_path, res.stdout.strip()))
    except Exception:
        return False

    # Check for merge
    merge_head = os.path.join(git_dir, "MERGE_HEAD")
    if os.path.exists(merge_head):
        return True

    # Check for rebase
    rebase_merge = os.path.join(git_dir, "rebase-merge")
    rebase_apply = os.path.join(git_dir, "rebase-apply")
    if os.path.exists(rebase_merge) or os.path.exists(rebase_apply):
        return True

    return False


def ensure_git_repository_health(dir_path: str, force: bool = False) -> None:
    """Validates that the Git repository at dir_path is healthy and compatible with drift."""
    if force:
        return
    if is_bare_repository(dir_path):
        raise RuntimeError("Bare Git repositories are not supported for drift workspace.")
    if is_detached_head(dir_path):
        raise RuntimeError("Git repository is in a detached HEAD state.")
    if is_merge_or_rebase_in_progress(dir_path):
        raise RuntimeError("Git repository is currently in the middle of a merge or rebase operation.")


def ensure_writable(path: str) -> None:
    """Ensures that the provided path is valid and read-writable."""
    curr = os.path.abspath(path)
    while curr:
        if os.path.exists(curr):
            if os.path.isdir(curr) and os.access(curr, os.W_OK | os.X_OK):
                return
            else:
                raise PermissionError(f"Path '{path}' (resolved at '{curr}') is not writable.")
        parent = os.path.dirname(curr)
        if parent == curr:  # Root reached
            break
        curr = parent
    raise ValueError(f"Path '{path}' is invalid.")


def check_existing_workspace_status(drift_root: str) -> bool:
    """Checks if a complete and valid drift workspace already exists.

    Returns:
        True if a valid and healthy drift workspace is fully initialized.
        False if any part is missing, invalid, or corrupt.
    """
    config_file = os.path.join(drift_root, "config", "drift.toml")
    if not os.path.exists(config_file):
        return False

    # Validate drift.toml syntax & load it
    from .toml_parser import parse_toml
    from .workspace_config import load_workspace_config
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            content = f.read()
        data = parse_toml(content)
        if "workspace" not in data or "packages" not in data:
            return False
        # Validate workspace_config validation
        load_workspace_config(config_file)
    except Exception:
        return False

    # Check install/state.toml exists and has valid TOML syntax
    state_file = os.path.join(drift_root, "install", "state.toml")
    if not os.path.exists(state_file):
        return False
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state_content = f.read()
        parse_toml(state_content)
    except Exception:
        return False

    # Check render/ and install/ directories exist and are healthy git repos
    render_dir = os.path.join(drift_root, "render")
    install_dir = os.path.join(drift_root, "install")
    if not os.path.isdir(render_dir) or not os.path.isdir(install_dir):
        return False

    if not is_git_tracked(render_dir) or not is_git_tracked(install_dir):
        return False

    if is_bare_repository(render_dir) or is_bare_repository(install_dir):
        return False

    return True
