"""Feature implementation for initializing a drift workspace."""

import os
import sys
import subprocess

from .check_repo import (
    is_git_tracked,
    get_drift_root,
    ensure_writable,
    ensure_git_repository_health,
    check_existing_workspace_status,
)

def git_init_repo(dir_path: str, name: str) -> bool:
    """Initializes a git repository at dir_path.

    Raises RuntimeError if initialization fails, returns True on success.
    """
    os.makedirs(dir_path, exist_ok=True)
    try:
        subprocess.run(
            ["git", "init"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to initialize {name} git repository: {e.stderr}")


def append_to_gitignore(drift_root: str, folders_to_ignore: list) -> None:
    """Appends folders to .gitignore if they are not already ignored."""
    gitignore_path = os.path.join(drift_root, ".gitignore")
    existing_content = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            existing_content = f.read()

    new_ignores = []
    lines = existing_content.splitlines()
    normalized_lines = {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}

    for folder in folders_to_ignore:
        if folder not in normalized_lines and folder.rstrip("/") not in normalized_lines:
            new_ignores.append(folder)

    if new_ignores:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing_content and not existing_content.endswith("\n"):
                f.write("\n")
            f.write("# drift workspace folders\n")
            for folder in new_ignores:
                f.write(f"{folder}\n")


def get_default_drift_toml_content() -> str:
    """Gets the default drift.toml template content, with an embedded fallback."""
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "drift_default.toml")
    if os.path.exists(template_path):
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"⚠️ Warning: Default drift.toml template file is unreadable: {e}. Using minimal fallback configuration.", file=sys.stderr)
    else:
        print("⚠️ Warning: Default drift.toml template file is missing. Using minimal fallback configuration.", file=sys.stderr)

    # Fallback to hardcoded minimal content to ensure self-containment
    return """# =====================================================================
# drift.toml Minimal Configuration
# =====================================================================

[workspace]
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[render.mustache]
input_file = "mustache.envst.json"
suffix = "mustache"
render_command = "mustache %i %s"

[packages.enable]
DEFAULT = false
"""


def init_drift_workspace(drift_root: str, force: bool = False, no_git_root: bool = False) -> None:
    """Initializes the active repository as a drift workspace.

    Only works if the directory is empty or tracked by git, unless force is True.
    """
    # 1. Ensure the provided drift_root path is valid and read-writable
    ensure_writable(drift_root)

    # 2. Check if the directory is tracked by git
    is_git = is_git_tracked(drift_root)

    if not is_git:
        # Check if directory exists and is not empty
        if not force and os.path.exists(drift_root) and os.listdir(drift_root):
            raise RuntimeError("Directory is not empty and not tracked by git.")

        # If directory is empty and not tracked by git, init an empty git repo
        git_init_repo(drift_root, "main")
        is_git = True

    # 3. Change to git root, and check git health unless force is True
    if not no_git_root:
        drift_root = get_drift_root(drift_root, force=force)

    # Validate main git repo health (bare, detached head, merge/rebase in progress)
    ensure_git_repository_health(drift_root, force=force)

    # Check if already initialized or partially initialized
    if not force:
        if check_existing_workspace_status(drift_root):
            raise RuntimeError(f"drift workspace is already initialized in '{drift_root}'.")

        # Check if partially initialized (any of the core files or folders exist)
        config_file = os.path.join(drift_root, "config", "drift.toml")
        render_dir = os.path.join(drift_root, "render")
        install_dir = os.path.join(drift_root, "install")

        if os.path.exists(config_file) or os.path.isdir(render_dir) or os.path.isdir(install_dir):
            raise RuntimeError(
                f"drift workspace exists at '{drift_root}' but has an invalid or corrupt configuration. "
                f"Use --force to overwrite and re-initialize."
            )

    # 4. Creates .gitignore entries to isolate render/ and install/ folders.
    append_to_gitignore(drift_root, ["render/", "install/"])

    # 5. Initializes render/ and install/ as independent, untracked local Git repositories.
    render_dir = os.path.join(drift_root, "render")
    install_dir = os.path.join(drift_root, "install")

    git_init_repo(render_dir, "render")
    git_init_repo(install_dir, "install")

    # Generate extra .stow-local-ignore at root of install/
    stow_ignore_path = os.path.join(install_dir, ".stow-local-ignore")
    with open(stow_ignore_path, "w", encoding="utf-8") as f:
        f.write("state.toml\n")

    # 6. Creates default directory templates (src/, config/drift.toml, install/state.toml)
    os.makedirs(os.path.join(drift_root, "src"), exist_ok=True)
    config_dir = os.path.join(drift_root, "config")
    os.makedirs(config_dir, exist_ok=True)

    config_file = os.path.join(config_dir, "drift.toml")
    with open(config_file, "w", encoding="utf-8") as f:
        f.write(get_default_drift_toml_content())

    # Write install/state.toml
    state_file = os.path.join(install_dir, "state.toml")
    with open(state_file, "w", encoding="utf-8") as f:
        f.write("[packages]\n")
