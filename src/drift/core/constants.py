"""Global constants for the drift dotfiles manager."""

import json
import os
import sys
from enum import Enum, IntEnum
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Union, Tuple

def add_suffix_filename(filename: str, suffix: str) -> str:
    """Returns a filename string with the given suffix injected before the file extension.

    Example:
        add_suffix_filename("drift_workspace.toml", "local")
        # -> "drift_workspace.local.toml"
    """
    if "/" in filename or "\\" in filename:
        raise ValueError(
            f"add_suffix_filename expects a bare filename without path separators, got: '{filename}'"
        )
    stem, ext = os.path.splitext(filename)
    return f"{stem}.{suffix}{ext}"

def add_local_filename(filename: str) -> str:
    """Appends '.local' before the file extension of a filename string."""
    return add_suffix_filename(filename, 'local')

def add_envst_filename(filename: str) -> str:
    """Appends '.envst' before the file extension of a filename string."""
    return add_suffix_filename(filename, 'envst')

def add_suffix_path(file: Path, suffix: str) -> Path:
    """Returns a new Path with the given suffix injected before the file extension.

    Example:
        add_suffix_path(Path("config/drift_workspace.toml"), "local")
        # -> Path("config/drift_workspace.local.toml")
    """
    return file.with_name(add_suffix_filename(file.name, suffix))

def add_local_path(file: Path) -> Path:
    """Appends '.local' before the file extension of a Path object."""
    return add_suffix_path(file, 'local')

def add_envst_path(file: Path) -> Path:
    """Appends '.envst' before the file extension of a Path object."""
    return add_suffix_path(file, 'envst')


CONFIG_DIR_NAME = "config"
WORKSPACE_CONFIG_FILE_NAME = "drift_workspace.toml"
WORKSPACE_CONFIG_LOCAL_FILE_NAME = add_local_filename(WORKSPACE_CONFIG_FILE_NAME)
CURRENT_WORKSPACE_CONFIG_FILE_NAMES = (
    WORKSPACE_CONFIG_FILE_NAME,
    add_envst_filename(WORKSPACE_CONFIG_FILE_NAME),
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    add_envst_filename(WORKSPACE_CONFIG_LOCAL_FILE_NAME),
)
LEGACY_WORKSPACE_CONFIG_FILE_NAMES = (
    "drift.toml",
    "drift.local.toml",
    "drift.envst.toml",
    "drift.local.envst.toml",
)
# Any of these files under '<root>/config/' marks '<root>' as a drift workspace root.
# Mirrors the candidates accepted by check_workspace_config (legacy names included so
# that 'drift repair' can still locate and migrate legacy workspaces).
WORKSPACE_ROOT_MARKER_FILE_NAMES = CURRENT_WORKSPACE_CONFIG_FILE_NAMES + LEGACY_WORKSPACE_CONFIG_FILE_NAMES
PACKAGE_CONFIG_FILE_NAME = "drift_package.toml"
PACKAGE_CONFIG_LOCAL_FILE_NAME = "drift_package.local.toml"
SECRETS_ENV_FILE_NAME = "secrets.env"
DRIFT_IGNORE_FILE_NAME = ".drift_ignore"
DRIFT_IGNORE_LEGACY_FILE_NAME = ".driftignore"
DRIFT_IGNORE_FILE_NAME_LIST = (DRIFT_IGNORE_FILE_NAME, DRIFT_IGNORE_LEGACY_FILE_NAME)
DRIFT_KEEP_FILE_NAME = ".drift_keep"
STATE_REGISTRY_FILE_NAME = "state.toml"
DRIFT_INTERNAL_DIR_NAME = ".drift"
DRIFT_HOOKS_DIR_NAME = "drift_hooks"
DRIFT_INTERNAL_HOOKS_DIR_NAME = "hooks"
DRIFT_INTERNAL_RENDER_DIR_NAME = "render"
DRIFT_INTERNAL_WORKSPACE_INPUT_DIR_NAME = "workspace"
DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME = "package"
RENDER_LOCK_FILE_NAME = "render_lock.json"
FORBIDDEN_RENDER_ENGINE_SUFFIXES: frozenset = frozenset({
    "drift_package",
    "drift_hook",
    "drift_ignore",
    "drift_workspace",
    "drift_hooks",
    "drift",
})
INTERNAL_RENDER_COMMAND = "internal"
DEFAULT_PACKAGE_HOOK_FILE_NAME = "drift_package.py"
PACKAGE_HOOK_FUNCTION_NAME = "configure_package"
FORBIDDEN_PACKAGE_NAMES: frozenset = frozenset({
    CONFIG_DIR_NAME,
    "install",
    "render",
    "src",
    "backup",
    STATE_REGISTRY_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME,
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_HOOKS_DIR_NAME,
    ".git",
    ".gitignore",
})
MIDWAY_TRANSACTION_STATES: frozenset = frozenset({"staging", "installing"})
ROLLBACK_ELIGIBLE_STATES: frozenset = frozenset({"staging", "staged", "installing"})

WINDOWS_PLATFORM_ALIASES = ("windows", "win32", "winos", "win")
WINDOWS_OS_ALIASES = WINDOWS_PLATFORM_ALIASES

DEFAULT_WORKSPACE_HOOK_FILE_NAME = "drift_workspace.py"
WORKSPACE_HOOK_FUNCTION_NAME = "configure_workspace"

DRIFT_PACKAGE_FACT_KEYS: FrozenSet[str] = frozenset({
    "drift_package_name",
    "drift_package_source_dir",
    "drift_package_src_dir",
    "drift_package_render_dir",
    "drift_package_install_dir",
    "drift_package_target_dir",
    "drift_package_install_method",
})


class PackageStage(str, Enum):
    """Enumeration of package stages and workspace directory bases."""
    SOURCE = "source"
    INSTALL = "install"

    def __str__(self) -> str:
        return self.value

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

    def __str__(self) -> str:
        return self.value


class InstallMethod(str, Enum):
    """Supported package installation methods for deploying configuration files."""
    DEFAULT = "symlink"
    SYMLINK = "symlink"
    COPY = "copy"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def default(cls) -> "InstallMethod":
        """Returns the default installation method (SYMLINK)."""
        return cls.DEFAULT

    @classmethod
    def _missing_(cls, value):
        if value is None:
            return cls.DEFAULT
        if isinstance(value, str) and value.strip().lower() in ("stow", "link", "default"):
            return cls.DEFAULT
        return super()._missing_(value)

    @classmethod
    def from_str(cls, val: Optional[Union[str, "InstallMethod"]] = None) -> "InstallMethod":
        """Parses a string or InstallMethod instance into an InstallMethod enum member.

        If val is None, returns the default installation method (DEFAULT / SYMLINK).
        """
        if val is None:
            return cls.DEFAULT
        if isinstance(val, cls):
            return val
        s = str(val).strip().lower()
        if s in ("symlink", "stow", "link", "default"):
            return cls.SYMLINK
        if s == "copy":
            return cls.COPY
        raise ValueError(f"Unknown install method '{val}'. Valid choices: 'symlink', 'copy'.")


class RollbackPolicy(str, Enum):
    """Controls automatic rollback behavior when a deployment or installation fails midway."""
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def default(cls) -> "RollbackPolicy":
        """Returns the default rollback policy (AUTO)."""
        return cls.AUTO

    @classmethod
    def _missing_(cls, value):
        if value is None:
            return cls.AUTO
        if isinstance(value, bool):
            return cls.ALWAYS if value else cls.NEVER
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("auto", "default"):
                return cls.AUTO
            if s in ("always", "yes", "true", "1", "on"):
                return cls.ALWAYS
            if s in ("never", "no", "false", "0", "off"):
                return cls.NEVER
        return super()._missing_(value)

    @classmethod
    def from_val(
        cls,
        val: Optional[Union[str, bool, "RollbackPolicy"]] = None,
        context: str = "",
    ) -> "RollbackPolicy":
        """Parses a string, boolean, or RollbackPolicy instance into a RollbackPolicy enum member."""
        if val is None:
            return cls.AUTO
        if isinstance(val, cls):
            return val
        if isinstance(val, bool):
            return cls.ALWAYS if val else cls.NEVER
        if isinstance(val, str):
            s = val.strip().lower()
            if s in ("auto", "default"):
                return cls.AUTO
            if s in ("always", "yes", "true", "1", "on"):
                return cls.ALWAYS
            if s in ("never", "no", "false", "0", "off"):
                return cls.NEVER
        ctx_str = f" for {context}" if context else ""
        raise ValueError(
            f"Invalid rollback policy {val!r}{ctx_str}. "
            "Valid choices: 'auto', 'always' ('yes'), 'never' ('no')."
        )


class BackupSubfolder(str, Enum):
    """Subfolders within backup/<pkg>/ for different backup categories."""
    OVERWRITTEN = "overwritten"
    DELETED = "deleted"
    WAL = "wal"
    STASH = "stash"

    def __str__(self) -> str:
        return self.value


WAL_ACTIONS_FILE_NAME = "actions.json"


class DirMode(str, Enum):
    """Controls directory inclusion in folder comparison and path listing operations."""
    NO_DIR = "no-dir"
    ONLY_EMPTY_DIR = "only-empty-dir"
    ALL_DIRS = "all-dirs"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_str(cls, val: Union[str, "DirMode"]) -> "DirMode":
        """Parses a string or DirMode instance into a DirMode enum member."""
        if isinstance(val, cls):
            return val
        if isinstance(val, str):
            s = val.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        valid_choices = ", ".join(repr(m.value) for m in cls)
        raise ValueError(f"Invalid dir_mode '{val}'. Must be one of ({valid_choices}).")


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

_DEFAULT_DRIFT_IGNORE_HEADER = (
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
    "# Default Drift Ignore List\n"
    "# ---------------------------------------------------------------------\n"
)

# Machine-generated junk (bytecode, tool caches, VCS metadata, editor temp files, OS metadata).
# Never meaningful configuration payload, so it is also filtered at render time
# (see list_folder_paths(ignore_junk_files=True)), not just at deploy time.
DEFAULT_DRIFT_IGNORE_JUNK_CONTENT = (
    "# Python bytecode and cache files\n"
    "__pycache__\n"
    "/__pycache__/\n"
    "\\.py[cod]$\n"
    "\\$py\\.class$\n"
    "\\.pytest_cache\n"
    "/\\.pytest_cache/\n"
    "\\.mypy_cache\n"
    "/\\.mypy_cache/\n"
    "\\.ruff_cache\n"
    "/\\.ruff_cache/\n"
    "\\.venv\n"
    "/\\.venv/\n"
    "^venv$\n"
    "/venv/\n"
    "\n"
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
)

# Deploy policy: package docs stay in render/ and install/ but are not delivered to the host.
# Users may remove these from a package .drift_ignore to deploy such files, so they must
# never be enforced at render time.
_DEFAULT_DRIFT_IGNORE_POLICY_CONTENT = (
    "# Package documentation and licenses\n"
    "^/README.*\n"
    "^/LICENSE.*\n"
    "^/COPYING.*\n"
    "\n"
    "# Drift internal control plane and sandbox\n"
    "^/\\.drift/\n"
    "^/\\.drift$\n"
)

DEFAULT_DRIFT_IGNORE_CONTENT = (
    _DEFAULT_DRIFT_IGNORE_HEADER + DEFAULT_DRIFT_IGNORE_JUNK_CONTENT + _DEFAULT_DRIFT_IGNORE_POLICY_CONTENT
)


def parse_ignore_pattern_lines(content: str) -> List[str]:
    """Returns the non-empty, non-comment lines of ignore file content as patterns."""
    return [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]


# Default list of ignore patterns generated from DEFAULT_DRIFT_IGNORE_CONTENT
DEFAULT_IGNORE_PATTERNS: List[str] = parse_ignore_pattern_lines(DEFAULT_DRIFT_IGNORE_CONTENT)

# Junk-only subset of DEFAULT_IGNORE_PATTERNS, safe to enforce at render time
DEFAULT_JUNK_IGNORE_PATTERNS: List[str] = parse_ignore_pattern_lines(DEFAULT_DRIFT_IGNORE_JUNK_CONTENT)

# Supported package lifecycle hook event names
LIFECYCLE_HOOK_NAMES = (
    "probe",
    "pre_source",
    "post_render",
    "pre_install",
    "post_install",
    "pre_update",
    "post_update",
    "pre_uninstall",
    "post_uninstall",
    "health",
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

# Pre-flight hooks permitted to execute during dry-run when hooks are explicitly enabled (--with-hooks)
DRY_RUN_ENABLED_HOOKS = (
    "probe",
    "pre_source",
)

HOOK_CONFIG_OPTIONS = (*LIFECYCLE_HOOK_NAMES, "timeout", "failure_is_fatal")
HOOK_CONFIG_OPTION_SET = set(HOOK_CONFIG_OPTIONS)

DEFAULT_HOOK_TIMEOUT: int = 120
DEFAULT_HOOK_TIMEOUT_SECONDS: int = DEFAULT_HOOK_TIMEOUT

IN_TEST_MODE: bool = os.environ.get("drift_test_mode", os.environ.get("DRIFT_TEST_MODE", "0")) == "1"

DEFAULT_HOOK_COMMON_ENVS: Dict[str, str] = {
    "drift_hook": "1",
    "drift_non_interactive": "1",
}

DEFAULT_HOOK_NON_INTERACTIVE_EXTERNAL_ENVS: Dict[str, str] = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "SYSTEMD_PAGER": "cat",
    "BAT_PAGER": "cat",
    "DEBIAN_FRONTEND": "noninteractive",
    "CI": "true",
}

DEFAULT_HOOK_NON_INTERACTIVE_ENVS: Dict[str, str] = {
    **DEFAULT_HOOK_COMMON_ENVS,
    **DEFAULT_HOOK_NON_INTERACTIVE_EXTERNAL_ENVS,
}

DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT = (
"""# =====================================================================
# drift_workspace.local.toml - Machine-Specific Configuration Overrides
# =====================================================================
# This file is gitignored and contains local overrides for drift_workspace.toml.

[workspace]
# default_target_directory = "~"

[packages.enable]
# gui_apps = false

# [settings]
# git_user_name = "Drift Bot"
# git_user_email = "drift@localhost"
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

DEFAULT_MUSTACHE_ENVST_JSON_CONTENT = json.dumps({
    "sample_theme": "${DRIFT_SAMPLE_ENV_THEME}",
    "sample_editor": "${DRIFT_SAMPLE_ENV_EDITOR}"
}, indent=4) + "\n"

DEFAULT_JINJA2_MUSTACHE_JSON_CONTENT = json.dumps({
    "sample_theme": "{{theme}}",
    "sample_editor": "{{editor}}",
    "sample_tool": "git"
}, indent=4) + "\n"

TEMPORARY_FILE_PATTERNS = (
    "*.gitignore*",
    "*__pycache__*",
    "*.py[cod]",
    "*$py.class",
    ".pytest_cache*",
    ".mypy_cache*",
    ".ruff_cache*",
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

DEFAULT_INTERNAL_GITIGNORE_CONTENT = (
    "# =====================================================================\n"
    "# .gitignore - Internal Repository Ignore Rules (render/ & install/)\n"
    "# =====================================================================\n"
    "# Python bytecode and cache files\n"
    "__pycache__/\n"
    "*.py[cod]\n"
    "*$py.class\n"
    ".venv/\n"
    "venv/\n"
    ".pytest_cache/\n"
    ".mypy_cache/\n"
    ".ruff_cache/\n"
    "\n"
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

DEFAULT_INTERNAL_GITIGNORE_ENTRIES: Tuple[str, ...] = tuple(
    line.strip()
    for line in DEFAULT_INTERNAL_GITIGNORE_CONTENT.splitlines()
    if line.strip() and not line.strip().startswith("#")
)

DEFAULT_ROOT_GITIGNORE_ENTRIES: Tuple[str, ...] = (
    "render/",
    "install/",
    "*.local.toml",
    f"{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}",
    *DEFAULT_INTERNAL_GITIGNORE_ENTRIES,
)


def get_default_internal_gitignore_content() -> str:
    """Gets default .gitignore template content for internal repositories (render/ & install/)."""
    return DEFAULT_INTERNAL_GITIGNORE_CONTENT


def _load_bundled_template(template_filename: str, description: str) -> str:
    """Loads a bundled template file with universal-newline normalization ('\\r\\n' -> '\\n').

    Ensures that raw bytes read via pkgutil.get_data on Windows checkouts (CRLF)
    never contain '\\r\\n' before being written via Path.write_text() (which would
    otherwise produce corrupted '\\r\\r\\n' line endings on Windows).
    """
    try:
        import pkgutil
        data = pkgutil.get_data("drift", f"templates/{template_filename}")
        if data is not None:
            return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (ImportError, OSError, LookupError):
        pass

    template_path = Path(__file__).resolve().parent.parent / "templates" / template_filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    raise FileNotFoundError(f"{description} not found at {template_path}")


def get_default_package_config_content(
    package_name: str,
    install_method: InstallMethod = InstallMethod.DEFAULT,
    target_directory: Optional[Path] = None,
    config_filename: str = PACKAGE_CONFIG_FILE_NAME,
) -> str:
    """Renders the default drift_package.toml content for a package."""
    template_str = _load_bundled_template(
        "drift_package_default.toml",
        "Default drift_package.toml template file",
    )

    if target_directory is None:
        target_directory_line = '# target_directory = "~"   # Destination for this package'
    else:
        target_directory_line = f'target_directory = "{target_directory.as_posix()}"   # Destination for this package'

    return (
        template_str
        .replace("{package_name}", package_name)
        .replace("{config_filename}", config_filename)
        .replace("{install_method}", str(install_method))
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


def get_default_mustache_content() -> str:
    """Gets default mustache.envst.json template content."""
    return DEFAULT_MUSTACHE_ENVST_JSON_CONTENT


def get_default_jinja2_content() -> str:
    """Gets default jinja2.mustache.json template content."""
    return DEFAULT_JINJA2_MUSTACHE_JSON_CONTENT


def get_default_drift_workspace_toml_content() -> str:
    """Gets the default drift_workspace.toml template content."""
    return _load_bundled_template(
        "drift_workspace_default.toml",
        "Default drift_workspace.toml template file",
    )


def get_default_package_hook_content(package_name: str) -> str:
    """Gets the default drift_package.py template content for a package."""
    template_str = _load_bundled_template(
        "drift_package_default.py",
        "Default drift_package.py template file",
    )
    return template_str.replace("{package_name}", package_name)


def get_default_workspace_hook_content() -> str:
    """Gets the default drift_workspace.py template content."""
    return _load_bundled_template(
        "drift_workspace_default.py",
        "Default drift_workspace.py template file",
    )


def configure_utf8_streams() -> None:
    """Configures sys.stdout and sys.stderr to use UTF-8 encoding with replacement error handler.

    Ensures safe Unicode and emoji printing across all platforms, especially Windows console/pipe.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, AttributeError, ValueError):
                pass


# Initialize UTF-8 stream encoding immediately upon module import
configure_utf8_streams()


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



