"""Global constants for the drift dotfiles manager."""

CONFIG_DIR_NAME = "config"
GLOBAL_CONFIG_FILE_NAME = "drift.toml"
PACKAGE_CONFIG_FILE_NAME = "drift_package.toml"
SECRETS_ENV_FILE_NAME = "secrets.env"
PACKAGE_CONFIG_FILE_NAME_LIST = [PACKAGE_CONFIG_FILE_NAME]
PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST = ["drift_package.local.toml"]
DRIFT_IGNORE_FILE_NAME = ".drift_ignore"
MANAGED_CONFIG_FILES = [PACKAGE_CONFIG_FILE_NAME, DRIFT_IGNORE_FILE_NAME, ".stow-local-ignore"]

import os
from typing import List

IN_TEST_MODE: bool = os.environ.get("DRIFT_TEST_MODE", "0") == "1"

INITIAL_ENV: List[str] = list(os.environ.keys())

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

def set_test_mode(enabled: bool) -> None:
    global IN_TEST_MODE
    IN_TEST_MODE = enabled

def in_test_mode() -> bool:
    return IN_TEST_MODE

