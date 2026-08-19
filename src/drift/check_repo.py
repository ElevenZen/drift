"""Repository validation and health checks using pathlib."""

import logging
from pathlib import Path

from .git_utils import (
    is_git_tracked,
    is_bare_repository,
)

logger = logging.getLogger(__name__)


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
