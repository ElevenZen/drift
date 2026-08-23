"""Feature implementation for initializing a drift workspace using pathlib."""

import json
import logging
import sys
import subprocess
from pathlib import Path

from .constants import (
    CONFIG_DIR_NAME,
    SECRETS_ENV_FILE_NAME,
    GLOBAL_CONFIG_FILE_NAME,
    GLOBAL_CONFIG_LOCAL_FILE_NAME,
    get_default_drift_toml_content,
    get_default_drift_local_toml_content,
    get_default_secrets_env_content,
    get_default_envsubst_content,
    get_default_mustache_content,
    get_default_jinja2_content,
)
from .check_repo import check_existing_workspace_status, ComponentStatus
from .git_utils import (
    is_git_tracked,
    get_drift_root,
    ensure_git_repository_health,
    git_init_repo,
    append_to_gitignore,
)
from .file_utils import ensure_directory_writable


logger = logging.getLogger(__name__)


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
        report = check_existing_workspace_status(drift_root)
        if report.overall_status == ComponentStatus.GOOD:
            raise RuntimeError(f"drift workspace is already initialized in '{drift_root}'.")
        elif report.overall_status == ComponentStatus.BROKEN:
            raise RuntimeError(
                f"drift workspace at '{drift_root}' is partially initialized or has broken components:\n"
                f"{report.format_diagnostic_summary()}\n\n"
                f"👉 Run 'drift repair' to safely fix missing or broken components.\n"
                f"👉 Run 'drift init --force' to completely overwrite and re-initialize."
            )

    # 4. Creates .gitignore entries to isolate render/ and install/ folders and local-only config overrides.
    append_to_gitignore(drift_root, [
        "render/",
        "install/",
        "*.local.toml",
        f"{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}"
        ])

    # 5. Initializes render/ and install/ as independent, untracked local Git repositories.
    render_dir = drift_root / "render"
    install_dir = drift_root / "install"

    git_init_repo(render_dir, "render")
    git_init_repo(install_dir, "install")

    # Generate extra .stow-local-ignore at root of install/
    stow_ignore_path = install_dir / ".stow-local-ignore"
    stow_ignore_path.write_text("state.toml\n", encoding="utf-8")

    # 6. Creates default directory templates (src/, config/drift.toml, config/drift.local.toml, install/state.toml)
    (drift_root / "src").mkdir(parents=True, exist_ok=True)
    config_dir = drift_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    config_file = config_dir / GLOBAL_CONFIG_FILE_NAME
    config_file.write_text(get_default_drift_toml_content(), encoding="utf-8")

    # Create drift.local.toml template
    local_config_file = config_dir / GLOBAL_CONFIG_LOCAL_FILE_NAME
    if not local_config_file.exists() or force:
        local_config_file.write_text(get_default_drift_local_toml_content(), encoding="utf-8")

    # Create secrets.env template
    secrets_file = config_dir / SECRETS_ENV_FILE_NAME
    if not secrets_file.exists() or force:
        secrets_file.write_text(get_default_secrets_env_content(), encoding="utf-8")

    # Create empty envsubst.bash, mustache.envst.json, and jinja2.mustache.json as referenced in default drift.toml
    envsubst_input = config_dir / "envsubst.bash"
    if not envsubst_input.exists():
        envsubst_input.write_text(get_default_envsubst_content(), encoding="utf-8")
    else:
        logger.warning(f"envsubst.bash already exists at '{envsubst_input}', skipping creation.")

    mustache_input = config_dir / "mustache.envst.json"
    if not mustache_input.exists():
        mustache_input.write_text(get_default_mustache_content(), encoding="utf-8")
    else:
        logger.warning(f"mustache.envst.json already exists at '{mustache_input}', skipping creation.")

    jinja2_input = config_dir / "jinja2.mustache.json"
    if not jinja2_input.exists():
        jinja2_input.write_text(get_default_jinja2_content(), encoding="utf-8")
    else:
        logger.warning(f"jinja2.mustache.json already exists at '{jinja2_input}', skipping creation.")

    # Write install/state.toml
    state_file = install_dir / "state.toml"
    state_file.write_text("[packages]\n", encoding="utf-8")
