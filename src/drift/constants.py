"""Global constants for the drift dotfiles manager."""

import json
import os
import sys
from enum import Enum, IntEnum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

CONFIG_DIR_NAME = "config"
WORKSPACE_CONFIG_FILE_NAME = "drift_workspace.toml"
WORKSPACE_CONFIG_LOCAL_FILE_NAME = "drift_workspace.local.toml"
LEGACY_WORKSPACE_CONFIG_FILE_NAMES = (
    "drift.toml",
    "drift.local.toml",
    "drift.envst.toml",
    "drift.local.envst.toml",
)
PACKAGE_CONFIG_FILE_NAME = "drift_package.toml"
PACKAGE_CONFIG_LOCAL_FILE_NAME = "drift_package.local.toml"
PACKAGE_CONFIG_FILE_NAME_LIST = [PACKAGE_CONFIG_FILE_NAME]
SECRETS_ENV_FILE_NAME = "secrets.env"
DRIFT_IGNORE_FILE_NAME = ".drift_ignore"
DRIFT_IGNORE_LEGACY_FILE_NAME = ".driftignore"
DRIFT_IGNORE_FILE_NAME_LIST = (DRIFT_IGNORE_FILE_NAME, DRIFT_IGNORE_LEGACY_FILE_NAME)
STOW_LOCAL_IGNORE_FILE_NAME = ".stow-local-ignore"
STATE_REGISTRY_FILE_NAME = "state.toml"
INSTALL_STOW_IGNORE_PATTERN = r"^/state\.toml"
INTERNAL_RENDER_COMMAND = "internal"
DRIFT_GENERATED_FILES = (STOW_LOCAL_IGNORE_FILE_NAME,)
DEFAULT_PACKAGE_HOOK_FILE_NAME = "drift_package.py"
PACKAGE_HOOK_FUNCTION_NAME = "configure_package"

MANAGED_CONFIG_FILES = (PACKAGE_CONFIG_FILE_NAME,
                        PACKAGE_CONFIG_LOCAL_FILE_NAME,
                        DEFAULT_PACKAGE_HOOK_FILE_NAME,
                        DRIFT_IGNORE_FILE_NAME,
                        *DRIFT_GENERATED_FILES,)
FORBIDDEN_PACKAGE_NAMES = (
    CONFIG_DIR_NAME,
    "install",
    "render",
    "src",
    "backup",
    STATE_REGISTRY_FILE_NAME,
    STOW_LOCAL_IGNORE_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME,
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
    ".git",
    ".gitignore",
)
MIDWAY_TRANSACTION_STATES = ("staging", "deploying")

WINDOWS_PLATFORM_ALIASES = ("windows", "win32", "winos", "win")
WINDOWS_OS_ALIASES = WINDOWS_PLATFORM_ALIASES

DEFAULT_WORKSPACE_HOOK_FILE_NAME = "drift_workspace.py"
WORKSPACE_HOOK_FUNCTION_NAME = "configure_workspace"


class PackageStage(str, Enum):
    """Enumeration of package stages and workspace directory bases."""
    SOURCE = "source"
    INSTALL = "install"

    @classmethod
    def from_str(cls, val: Union[str, "PackageStage"]) -> "PackageStage":
        """Parses a stage string (e.g. 'source', 'src', 'install') into a PackageStage member."""
        if isinstance(val, cls):
            return val
        s = str(val).strip().lower()
        if s in ("source", "src"):
            return cls.SOURCE
        if s in ("install", "installed"):
            return cls.INSTALL
        raise ValueError(f"Unknown package stage '{val}'. Valid choices: 'source', 'install'.")


class LineEnding(str, Enum):
    """Line ending modes for text files."""
    LF = "lf"
    CRLF = "crlf"
    PRESERVE = "preserve"


class ExitCode(IntEnum):
    """Standardized exit codes for the Drift CLI and automated pipeline integration."""
    SUCCESS = 0
    GENERAL_ERROR = 1
    CONFIG_ERROR = 2
    DRIFT_DETECTED = 3
    RENDER_ERROR = 4
    COLLISION_ERROR = 5
    HEALTH_CHECK_FAILED = 6
    HOOK_SKIPPED = 7

DEFAULT_DRIFT_IGNORE_CONTENT = (
    "# =====================================================================\n"
    "# .drift_ignore - PCRE Regex Package Ignore Patterns\n"
    "# =====================================================================\n"
    "# Lines starting with '#' or empty lines are ignored.\n"
    "# Patterns use Perl-Compatible Regular Expressions (PCRE).\n"
    "# Note: In source packages, hidden files/dirs are named with 'dot-'\n"
    "# (e.g. 'dot-config/' instead of '.config/').\n"
    "#\n"
    "# Matching Rules (GNU Stow Algorithm):\n"
    "# Drift splits regex patterns into two groups:\n"
    "# 1. Patterns containing '/':\n"
    "#    Matched against relative path starting with '/' (e.g. '/sub/file.txt').\n"
    "#    To match a file at package root, use '^/sample\\.txt$' (do NOT use './').\n"
    "#    Example: ^/sample\\.txt$\n"
    "#    Example: ^/dot-config/coc-settings\\.json$\n"
    "#    Example: /cache/\n"
    "#\n"
    "# 2. Patterns WITHOUT '/':\n"
    "#    Matched against the file/directory basename anywhere in the package.\n"
    "#    Example: \\.bak$   (matches any file ending in .bak)\n"
    "#    Example: ^~       (matches temporary files starting with ~)\n"
    "# ---------------------------------------------------------------------\n"
    "# Default Stow Ignore List\n"
    "# ---------------------------------------------------------------------\n"
    "# Version control systems & ignore metadata\n"
    "^/\\.gitignore\n"
    "\\.gitignore\n"
    "\\.git\n"
    "\\.hg\n"
    "\\.svn\n"
    "_darcs\n"
    "CVS\n"
    "\\.cvsignore\n"
    "RCS\n"
    "\\.+,v\n"
    "\\.\\#.+\n"
    "\n"
    "# Editor temporary and backup files\n"
    ".+~\n"
    "\\#.*\\#\n"
    ".*\\.sw[a-p]$\n"
    ".*\\.swp$\n"
    ".*\\.swo$\n"
    ".*\\.un~$\n"
    "\n"
    "# OS metadata\n"
    "^\\.DS_Store$\n"
    "^Thumbs\\.db$\n"
    "\n"
    "# Package documentation and licenses\n"
    "^/README.*\n"
    "^/LICENSE.*\n"
    "^/COPYING.*\n"
)

# Default list of ignore patterns generated from DEFAULT_DRIFT_IGNORE_CONTENT for GNU Stow matching
DEFAULT_STOW_IGNORE_PATTERNS: List[str] = [
    line.strip()
    for line in DEFAULT_DRIFT_IGNORE_CONTENT.splitlines()
    if line.strip() and not line.strip().startswith("#")
]

# Lifecycle hooks categorized by their execution working directory (CWD)
SOURCE_CWD_HOOK_NAMES = (
    "probe",
    "pre_source",
)

RENDER_CWD_HOOK_NAMES = (
    "post_render",
)

INSTALL_CWD_HOOK_NAMES = (
    "pre_install",
    "pre_update",
    "post_uninstall",
)

TARGET_CWD_HOOK_NAMES = (
    "post_install",
    "post_update",
    "pre_uninstall",
    "health",
)

LIFECYCLE_HOOK_NAMES = (
    *SOURCE_CWD_HOOK_NAMES,
    *RENDER_CWD_HOOK_NAMES,
    *INSTALL_CWD_HOOK_NAMES,
    *TARGET_CWD_HOOK_NAMES,
)

INSTALLATION_HOOK_NAMES = (
    "pre_install",
    "post_install",
    "pre_update",
    "post_update",
)

UNINSTALL_HOOK_NAMES = (
    "pre_uninstall",
    "post_uninstall",
)

HOOK_CONFIG_OPTIONS = (*LIFECYCLE_HOOK_NAMES, "timeout", "rollback_on_failure")
HOOK_CONFIG_OPTION_SET = set(HOOK_CONFIG_OPTIONS)

DEFAULT_HOOK_TIMEOUT: int = 120
DEFAULT_HOOK_TIMEOUT_SECONDS: int = DEFAULT_HOOK_TIMEOUT

IN_TEST_MODE: bool = os.environ.get("DRIFT_TEST_MODE", "0") == "1"

INITIAL_ENV: List[str] = list(os.environ.keys())

DEFAULT_HOOK_NON_INTERACTIVE_ENVS: Dict[str, str] = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "SYSTEMD_PAGER": "cat",
    "BAT_PAGER": "cat",
    "DEBIAN_FRONTEND": "noninteractive",
    "CI": "true",
    "DRIFT_HOOK": "1",
    "DRIFT_NON_INTERACTIVE": "1",
}

SYSTEM_FACT_KEYS: List[str] = [
    "drift_os",
    "drift_arch",
    "drift_distro",
    "drift_hostname",
    "drift_user",
    "drift_ip_addresses",
]

DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT = (
"""# =====================================================================
# drift_workspace.local.toml - Machine-Specific Configuration Overrides
# =====================================================================
# This file is gitignored and contains local overrides for drift_workspace.toml.

[workspace]
# default_target_directory = "~"

[packages.enable]
# gui_apps = false
"""
)
DEFAULT_DRIFT_LOCAL_TOML_CONTENT = DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT

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
    "export TEMPLATE_THEME=\"${DRIFT_SAMPLE_ENV_THEME:-UnixDefaultTheme}\"\n"
    "export TEMPLATE_EDITOR=\"${DRIFT_SAMPLE_ENV_EDITOR:-UnixDefaultEditor}\"\n"
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

TEMPORARY_FILE_PATTERNS = (
    "*.stow-local-ignore*",
    "*.gitignore*",
    "*~",
    "*#*#",
    "*.#*",
    "*.sw[a-p]",
    "*.swp",
    "*.swo",
    "*.un~",
    "*.DS_Store*",
    "*Thumbs.db*",
)

DEFAULT_DIFF_EXCLUDE_PATTERNS = tuple(f":(exclude){p}" for p in TEMPORARY_FILE_PATTERNS)

DEFAULT_INTERNAL_GITIGNORE_CONTENT = (
    "# =====================================================================\n"
    "# .gitignore - Internal Repository Ignore Rules (render/ & install/)\n"
    "# =====================================================================\n"
    "# Editor temporary, auto-save, and lock files\n"
    "*~\n"
    r"\#*\#" "\n"
    r".\#*" "\n"
    "\n"
    "# Vim / Neovim swap, undo, and backup files\n"
    "*.swp\n"
    "*.swo\n"
    r".*.sw[a-p]" "\n"
    "*.un~\n"
    "\n"
    "# OS metadata files\n"
    ".DS_Store\n"
    "Thumbs.db\n"
)


def get_default_internal_gitignore_content() -> str:
    """Gets default .gitignore template content for internal repositories (render/ & install/)."""
    return DEFAULT_INTERNAL_GITIGNORE_CONTENT


def get_default_package_config_content(
    package_name: str,
    install_method: str = "stow",
    target_directory: Optional[str] = None,
    config_filename: str = PACKAGE_CONFIG_FILE_NAME,
) -> str:
    """Renders the default drift_package.toml content for a package."""
    template_str: Optional[str] = None
    try:
        import pkgutil
        data = pkgutil.get_data("drift", "templates/drift_package_default.toml")
        if data:
            template_str = data.decode("utf-8")
    except Exception:
        pass

    if template_str is None:
        template_path = Path(__file__).resolve().parent / "templates" / "drift_package_default.toml"
        if template_path.exists():
            template_str = template_path.read_text(encoding="utf-8")
        else:
            raise FileNotFoundError(f"Default drift_package.toml template file not found at {template_path}")

    if target_directory is None:
        target_directory_line = '# target_directory = "~"   # Destination for this package'
    else:
        target_directory_line = f'target_directory = "{target_directory}"   # Destination for this package'

    return (
        template_str
        .replace("{package_name}", package_name)
        .replace("{config_filename}", config_filename)
        .replace("{install_method}", install_method)
        .replace("{target_directory_line}", target_directory_line)
    )


def get_default_drift_ignore_content() -> str:
    """Gets default .drift_ignore template content."""
    return DEFAULT_DRIFT_IGNORE_CONTENT


def get_default_drift_workspace_local_toml_content() -> str:
    """Gets default drift_workspace.local.toml template content."""
    return DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT


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


def get_default_drift_workspace_toml_content() -> str:
    """Gets the default drift_workspace.toml template content."""
    try:
        import pkgutil
        data = pkgutil.get_data("drift", "templates/drift_workspace_default.toml")
        if data:
            return data.decode("utf-8")
    except Exception:
        pass

    template_path = Path(__file__).resolve().parent / "templates" / "drift_workspace_default.toml"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    raise FileNotFoundError(f"Default drift_workspace.toml template file not found at {template_path}")


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


def inject_system_facts(probe_wan_ip: bool = False) -> None:
    """Injects auto-populated host facts into os.environ if not already set in INITIAL_ENV."""
    from .host_facts import get_system_facts
    facts = get_system_facts(probe_wan_ip=probe_wan_ip)
    for k, v in facts.items():
        if k not in INITIAL_ENV:
            os.environ[k] = v


