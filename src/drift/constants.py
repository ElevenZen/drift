"""Global constants for the drift dotfiles manager."""

from enum import IntEnum

CONFIG_DIR_NAME = "config"
GLOBAL_CONFIG_FILE_NAME = "drift.toml"
GLOBAL_CONFIG_LOCAL_FILE_NAME = "drift.local.toml"
PACKAGE_CONFIG_FILE_NAME = "drift_package.toml"
SECRETS_ENV_FILE_NAME = "secrets.env"
PACKAGE_CONFIG_FILE_NAME_LIST = [PACKAGE_CONFIG_FILE_NAME]
PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST = ["drift_package.local.toml"]
DRIFT_IGNORE_FILE_NAME = ".drift_ignore"
DRIFT_IGNORE_LEGACY_FILE_NAME = ".driftignore"
DRIFT_IGNORE_FILE_NAME_LIST = (DRIFT_IGNORE_FILE_NAME, DRIFT_IGNORE_LEGACY_FILE_NAME)
STOW_LOCAL_IGNORE_FILE_NAME = ".stow-local-ignore"
STATE_REGISTRY_FILE_NAME = "state.toml"
INSTALL_STOW_IGNORE_PATTERN = "^/state.toml"
INTERNAL_RENDER_COMMAND = "internal"
MANAGED_CONFIG_FILES = (PACKAGE_CONFIG_FILE_NAME, DRIFT_IGNORE_FILE_NAME, STOW_LOCAL_IGNORE_FILE_NAME,
                        *PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST)

WINDOWS_PLATFORM_ALIASES = ("windows", "win32", "winos", "win")
WINDOWS_OS_ALIASES = WINDOWS_PLATFORM_ALIASES

from enum import Enum, IntEnum


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

# This is the default list of ignore patterns used by GNU Stow when no custom .stow-local-ignore file is present in the package source.
DEFAULT_STOW_IGNORE_PATTERNS = [
    "RCS",
    r"\.+,v",
    "CVS",
    r"\.\#.+=",
    r"\.cvsignore",
    r"\.svn",
    "_darcs",
    r"\.hg",
    r"\.git",
    r"\.gitignore",
    r".+~",
    r"\#.*\#",
    r"^/README.*",
    r"^/LICENSE.*",
    r"^/COPYING.*",
]

# This is the default content for .stow-local-ignore when no custom ignore file is present in the package source.
# It contains common patterns which is used to ignore common VCS and editor files when using GNU Stow.
DEFAULT_STOW_IGNORE_CONTENT = (
    "# Comments and blank lines are allowed.\n"
    "RCS\n"
    r"\.+,v" "\n"
    "CVS\n"
    r"\.\#.+=" "\n"
    "# CVS conflict files / emacs lock files\n"
    r"\.cvsignore" "\n"
    r"\.svn" "\n"
    "_darcs\n"
    r"\.hg" "\n"
    r"\.git" "\n"
    r"\.gitignore" "\n"
    r".+~" "\n"
    "# emacs backup files\n"
    r"\#.*\#" "\n"
    "# emacs autosave files\n"
    r"^/README.*" "\n"
    r"^/LICENSE.*" "\n"
    r"^/COPYING.*" "\n"
)

LIFECYCLE_HOOK_NAMES = (
    "probe",
    "pre_source",
    "pre_install",
    "post_install",
    "pre_update",
    "post_update",
    "pre_uninstall",
    "post_uninstall",
    "post_render",
    "health",
)

UNINSTALL_HOOK_NAMES = (
    "pre_uninstall",
    "post_uninstall",
)

INSTALL_CWD_HOOK_NAMES = (
    "pre_install",
    "pre_update",
    "pre_uninstall",
)

SUDO_ELIGIBLE_HOOKS = (
    "pre_install",
    "post_install",
    "pre_update",
    "post_update",
    "pre_uninstall",
    "post_uninstall",
)

DEFAULT_HOOK_TIMEOUT: int = 120
DEFAULT_HOOK_TIMEOUT_SECONDS: int = DEFAULT_HOOK_TIMEOUT

import json
import os
import sys
from pathlib import Path
from typing import List, Optional

IN_TEST_MODE: bool = os.environ.get("DRIFT_TEST_MODE", "0") == "1"

INITIAL_ENV: List[str] = list(os.environ.keys())

SYSTEM_FACT_KEYS: List[str] = [
    "drift_os",
    "drift_arch",
    "drift_distro",
    "drift_hostname",
    "drift_user",
    "drift_ip_addresses",
]

DEFAULT_DRIFT_LOCAL_TOML_CONTENT = (
"""# =====================================================================
# drift.local.toml - Machine-Specific Configuration Overrides
# =====================================================================
# This file is gitignored and contains local overrides for drift.toml.

[workspace]
# default_target_directory = "~"

[packages.enable]
# gui_apps = false
"""
)

DEFAULT_FALLBACK_DRIFT_TOML_CONTENT = (
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

[render.var]
suffix = "var"
render_command = "internal"

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

[settings]
# probe_wan_ip = false  # Outbound WAN internet route probing (disabled by default)
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
    "\n"
    "# Package documentation and licenses\n"
    "^/README.*\n"
    "^/LICENSE.*\n"
    "^/COPYING.*\n"
)

DEFAULT_PACKAGE_CONFIG_TEMPLATE = """# src/{package_name}/{config_filename}
[package]
install_method = "{install_method}"  # Options: "stow" (symlink) or "copy" (physical)
{target_directory_line}
# target_directory_windows = "~"  # Windows-specific destination override
# source_directory = "."          # Package root relative to package directory

# Advanced Flags
# sudo = false
# fully_controlled_dirs = []      # Sync deletions inside these directories
# enable_render = true
# enable_install = true

# Host Requirements & Prerequisites (Declarative checks; skip package if unmet)
[package.requirements]
# os = ["linux"]                # Allowed OS: "linux", "darwin", "windows", "freebsd"
# arch = ["x86_64", "aarch64"]  # Allowed Arch: "x86_64", "arm64", "aarch64"
# distro = ["arch", "ubuntu"]   # Allowed Linux Distro IDs
# binaries = ["git"]            # Required binaries in host $PATH
# env = ["WAYLAND_DISPLAY"]     # Required environment variables when starting drift
# ip = ["192.168.1.0/24"]       # Matches host LAN IP address (exact, CIDR, or wildcard)

# Package Environment Variables
[env.override]
# Highest-priority package variables (overrides workspace configs, secrets, and system facts; CLI environment takes precedence)
# SAMPLE_OVERRIDE_VAR = "custom_value"

[env.fallback]
# Baseline default values (applied only when a variable is unset across all other scopes)
# SAMPLE_FALLBACK_VAR = "default_value"

# Lifecycle Hooks (Optional, set to script path or "disable" to turn off)
[hooks]
# probe          = ""           # Pre-flight requirement check (Exit 0 = Met, Exit != 0 = Unmet)
# pre_source     = ""
# post_render    = ""
# pre_install    = ""
# post_install   = ""
# pre_update     = ""
# post_update    = ""
# pre_uninstall  = ""
# post_uninstall = ""
# health         = ""
# timeout        = 120

# Windows-Specific Lifecycle Hooks (Optional overrides, e.g. post_install = "disable")
[hooks.windows]
# probe          = ""
# pre_source     = ""
# post_render    = ""
# pre_install    = ""
# post_install   = ""
# pre_update     = ""
# post_update    = ""
# pre_uninstall  = ""
# post_uninstall = ""
# health         = ""
# timeout        = 120
"""


def get_default_package_config_content(
    package_name: str,
    install_method: str = "stow",
    target_directory: Optional[str] = None,
    config_filename: str = PACKAGE_CONFIG_FILE_NAME,
) -> str:
    """Renders the default drift_package.toml content for a package."""
    if target_directory is None:
        target_directory_line = '# target_directory = "~"   # Destination for this package'
    else:
        target_directory_line = f'target_directory = "{target_directory}"   # Destination for this package'

    return DEFAULT_PACKAGE_CONFIG_TEMPLATE.format(
        package_name=package_name,
        config_filename=config_filename,
        target_directory_line=target_directory_line,
        install_method=install_method
    )


def get_default_drift_ignore_content() -> str:
    """Gets default .drift_ignore template content."""
    return DEFAULT_DRIFT_IGNORE_CONTENT


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
    # Try pkgutil first (supports zipapp and installed packages)
    try:
        import pkgutil
        data = pkgutil.get_data("drift", "templates/drift_default.toml")
        if data:
            return data.decode("utf-8")
    except Exception:
        pass

    template_path = Path(__file__).resolve().parent / "templates" / "drift_default.toml"
    if template_path.exists():
        try:
            return template_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️ Warning: Default drift.toml template file is unreadable: {e}. Using minimal fallback configuration.", file=sys.stderr)
    else:
        print("⚠️ Warning: Default drift.toml template file is missing. Using minimal fallback configuration.", file=sys.stderr)

    # Fallback to hardcoded minimal content to ensure self-containment
    return DEFAULT_FALLBACK_DRIFT_TOML_CONTENT


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


