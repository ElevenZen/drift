"""Feature implementation for initializing a drift workspace using pathlib."""

import json
import logging
import sys
import subprocess
from pathlib import Path

from .check_repo import check_existing_workspace_status
from .git_utils import (
    is_git_tracked,
    get_drift_root,
    ensure_git_repository_health,
)
from .file_utils import ensure_directory_writable


logger = logging.getLogger(__name__)


def git_init_repo(dir_path: Path, name: str) -> bool:
    """Initializes a git repository at dir_path.

    Raises RuntimeError if initialization fails, returns True on success.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "init"],
            cwd=str(dir_path),
            capture_output=True,
            text=True,
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to initialize {name} git repository: {e.stderr}")


def append_to_gitignore(drift_root: Path, folders_to_ignore: list) -> None:
    """Appends folders to .gitignore if they are not already ignored."""
    gitignore_path = drift_root / ".gitignore"
    existing_content = ""
    if gitignore_path.exists():
        existing_content = gitignore_path.read_text(encoding="utf-8")

    new_ignores = []
    lines = existing_content.splitlines()
    normalized_lines = {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}

    for folder in folders_to_ignore:
        if folder not in normalized_lines and folder.rstrip("/") not in normalized_lines:
            new_ignores.append(folder)

    if new_ignores:
        with gitignore_path.open("a", encoding="utf-8") as f:
            if existing_content and not existing_content.endswith("\n"):
                f.write("\n")
            f.write("# drift workspace folders\n")
            for folder in new_ignores:
                f.write(f"{folder}\n")


def get_default_drift_toml_content() -> str:
    """Gets the default drift.toml template content, with an embedded fallback."""
    template_path = Path(__file__).resolve().parent / "templates" / "drift_default.toml"
    if template_path.exists():
        try:
            return template_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️ Warning: Default drift.toml template file is unreadable: {e}. Using minimal fallback configuration.", file=sys.stderr)
    else:
        print("⚠️ Warning: Default drift.toml template file is missing. Using minimal fallback configuration.", file=sys.stderr)

    # Fallback to hardcoded minimal content to ensure self-containment
    return (
"""# =====================================================================
# drift.toml Minimal Configuration
# =====================================================================

[env]
DRIFT_SAMPLE_ENV_THEME = "nord-dark"
DRIFT_SAMPLE_ENV_EDITOR = "vim"

[packages.enable]
DEFAULT = false

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[render.mustache]
input_file = "mustache.envst.json"
suffix = "mustache"
render_command = "mustache %i %s"

[render.jinja2]
input_file = "jinja2.mustache.json"
suffix = "j2"
render_command = "jinja2 %s %i"

[workspace]
default_target_directory = "~"
""")


def init_drift_workspace(drift_root: Path, force: bool = False, no_git_root: bool = False) -> None:
    """Initializes the active repository as a drift workspace.

    Only works if the directory is empty or tracked by git, unless force is True.
    """
    # 1. Ensure the provided drift_root path is valid and read-writable
    ensure_directory_writable(drift_root, sudo=False)

    # 2. Check if the directory is tracked by git
    is_git = is_git_tracked(drift_root)

    if not is_git:
        # Check if directory exists and is not empty
        if not force and drift_root.exists() and any(drift_root.iterdir()):
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
        config_file = drift_root / "config" / "drift.toml"
        render_dir = drift_root / "render"
        install_dir = drift_root / "install"

        if config_file.exists() or render_dir.is_dir() or install_dir.is_dir():
            raise RuntimeError(
                f"drift workspace exists at '{drift_root}' but has an invalid or corrupt configuration. "
                f"Use --force to overwrite and re-initialize."
            )

    # 4. Creates .gitignore entries to isolate render/ and install/ folders and local-only config overrides.
    append_to_gitignore(drift_root, ["render/", "install/", "*.local.toml", "secret.env"])

    # 5. Initializes render/ and install/ as independent, untracked local Git repositories.
    render_dir = drift_root / "render"
    install_dir = drift_root / "install"

    git_init_repo(render_dir, "render")
    git_init_repo(install_dir, "install")

    # Generate extra .stow-local-ignore at root of install/
    stow_ignore_path = install_dir / ".stow-local-ignore"
    stow_ignore_path.write_text("state.toml\n", encoding="utf-8")

    # 6. Creates default directory templates (src/, config/drift.toml, install/state.toml)
    (drift_root / "src").mkdir(parents=True, exist_ok=True)
    config_dir = drift_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = config_dir / "drift.toml"
    config_file.write_text(get_default_drift_toml_content(), encoding="utf-8")

    # Create empty envsubst.bash, mustache.envst.json, and jinja2.mustache.json as referenced in default drift.toml
    envsubst_input = config_dir / "envsubst.bash"
    if not envsubst_input.exists():
        env_content = (
            "#!/bin/bash\n"
            "# Propagates variables defined in the workspace config [env] section\n"
            "export TEMPLATE_THEME=\"${DRIFT_SAMPLE_ENV_THEME:-default-theme}\"\n"
            "export TEMPLATE_EDITOR=\"${DRIFT_SAMPLE_ENV_EDITOR:-default-editor}\"\n"
        )
        envsubst_input.write_text(env_content, encoding="utf-8")
    else:
        logger.warning(f"envsubst.bash already exists at '{envsubst_input}', skipping creation.")

    mustache_input = config_dir / "mustache.envst.json"
    mustache_input_json = {
        "sample_theme": "${TEMPLATE_THEME}",
        "sample_editor": "${TEMPLATE_EDITOR}"
    }
    if not mustache_input.exists():
        mustache_input.write_text(json.dumps(mustache_input_json, indent=4), encoding="utf-8")
    else:
        logger.warning(f"mustache.envst.json already exists at '{mustache_input}', skipping creation.")

    jinja2_input = config_dir / "jinja2.mustache.json"
    jinja2_input_json = {
        "sample_theme": "{{theme}}",
        "sample_editor": "{{editor}}",
        "sample_tool": "git"
    }
    if not jinja2_input.exists():
        jinja2_input.write_text(json.dumps(jinja2_input_json, indent=4), encoding="utf-8")
    else:
        logger.warning(f"jinja2.mustache.json already exists at '{jinja2_input}', skipping creation.")

    # Write install/state.toml
    state_file = install_dir / "state.toml"
    state_file.write_text("[packages]\n", encoding="utf-8")
