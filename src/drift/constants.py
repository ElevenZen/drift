"""Global constants for the drift dotfiles manager."""

CONFIG_DIR_NAME = "config"
GLOBAL_CONFIG_FILE_NAME = "drift.toml"
GLOBAL_CONFIG_LOCAL_FILE_NAME = "drift.local.toml"
PACKAGE_CONFIG_FILE_NAME = "drift_package.toml"
SECRETS_ENV_FILE_NAME = "secrets.env"
PACKAGE_CONFIG_FILE_NAME_LIST = [PACKAGE_CONFIG_FILE_NAME]
PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST = ["drift_package.local.toml"]
DRIFT_IGNORE_FILE_NAME = ".drift_ignore"
MANAGED_CONFIG_FILES = [PACKAGE_CONFIG_FILE_NAME, DRIFT_IGNORE_FILE_NAME, ".stow-local-ignore"]

import json
import os
import sys
from pathlib import Path
from typing import List

IN_TEST_MODE: bool = os.environ.get("DRIFT_TEST_MODE", "0") == "1"

INITIAL_ENV: List[str] = list(os.environ.keys())

DEFAULT_DRIFT_LOCAL_TOML_CONTENT = (
"""# =====================================================================
# drift.local.toml - Machine-Specific Configuration Overrides
# =====================================================================
# This file is gitignored and contains local overrides for drift.toml.

# [workspace]
# default_target_directory = "~"

# [packages.enable]
# gui_apps = false
"""
)

DEFAULT_SECRETS_ENV_CONTENT = (
    "# =====================================================================\n"
    "# config/secrets.env - Environment Secret Vault (Gitignored)\n"
    "# =====================================================================\n"
    "# Place private secrets, tokens, or environment keys in this file.\n"
    "# They will be temporarily injected into os.environ before compiling templates.\n"
    "# ---------------------------------------------------------------------\n"
    "# GITHUB_TOKEN=\"ghp_xxxxxxxxxxxxxxxxxxxx\"\n"
    "# OPENAI_API_KEY=\"sk-xxxxxxxxxxxxxxxxxxxx\"\n"
    "# PRIVATE_EMAIL=\"user@example.com\"\n"
)

DEFAULT_ENVSUBST_BASH_CONTENT = (
    "#!/bin/bash\n"
    "# Propagates variables defined in the workspace config [env] section\n"
    "export TEMPLATE_THEME=\"${DRIFT_SAMPLE_ENV_THEME:-default-theme}\"\n"
    "export TEMPLATE_EDITOR=\"${DRIFT_SAMPLE_ENV_EDITOR:-default-editor}\"\n"
)

DEFAULT_MUSTACHE_ENVST_JSON_CONTENT = json.dumps({
    "sample_theme": "${TEMPLATE_THEME}",
    "sample_editor": "${TEMPLATE_EDITOR}"
}, indent=4) + "\n"

DEFAULT_JINJA2_MUSTACHE_JSON_CONTENT = json.dumps({
    "sample_theme": "{{theme}}",
    "sample_editor": "{{editor}}",
    "sample_tool": "git"
}, indent=4) + "\n"


def get_default_drift_local_toml_content() -> str:
    """Gets default drift.local.toml template content."""
    return DEFAULT_DRIFT_LOCAL_TOML_CONTENT


def get_default_secrets_env_content() -> str:
    """Gets default secrets.env template content."""
    return DEFAULT_SECRETS_ENV_CONTENT


def get_default_envsubst_content() -> str:
    """Gets default envsubst.bash template content."""
    return DEFAULT_ENVSUBST_BASH_CONTENT


def get_default_mustache_content() -> str:
    """Gets default mustache.envst.json template content."""
    return DEFAULT_MUSTACHE_ENVST_JSON_CONTENT


def get_default_jinja2_content() -> str:
    """Gets default jinja2.mustache.json template content."""
    return DEFAULT_JINJA2_MUSTACHE_JSON_CONTENT


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

[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"
default_install_method = "stow"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "envsubst < {src} > {dest}"

[render.mustache]
input_file = "mustache.envst.json"
suffix = "mustache"
render_command = "mustache {input} {src} > {dest}"

[render.jinja2]
input_file = "jinja2.mustache.json"
suffix = "j2"
render_command = "j2 {src} {input} -o {dest}"
"""
    )


def update_initial_env() -> None:
    """Updates INITIAL_ENV with current keys in os.environ."""
    global INITIAL_ENV
    INITIAL_ENV.clear()
    INITIAL_ENV.extend(os.environ.keys())


def set_initial_env(keys: List[str]) -> None:
    """Sets INITIAL_ENV explicitly (useful for testing)."""
    global INITIAL_ENV
    INITIAL_ENV.clear()
    INITIAL_ENV.extend(keys)


def set_test_mode(enabled: bool, enable_logging: bool = False) -> None:
    """Configures test mode.

    When test mode is enabled without enable_logging=True, Python logging is disabled
    to prevent noisy log output during test execution.
    """
    global IN_TEST_MODE
    IN_TEST_MODE = enabled
    import logging
    if enabled and not enable_logging:
        logging.disable(logging.CRITICAL)
    else:
        logging.disable(logging.NOTSET)


def in_test_mode() -> bool:
    return IN_TEST_MODE


