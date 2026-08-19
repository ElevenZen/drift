"""Global constants for the drift dotfiles manager."""

CONFIG_DIR_NAME = "config"
GLOBAL_CONFIG_FILE_NAME = "drift.toml"
PACKAGE_CONFIG_FILE_NAME = "drift_package.toml"
PACKAGE_CONFIG_FILE_NAME_LIST = [PACKAGE_CONFIG_FILE_NAME, "package.toml"]
DRIFT_IGNORE_FILE_NAME = ".drift_ignore"
MANAGED_CONFIG_FILES = [PACKAGE_CONFIG_FILE_NAME, DRIFT_IGNORE_FILE_NAME, ".stow-local-ignore"]

import os
IN_TEST_MODE: bool = os.environ.get("DRIFT_TEST_MODE", "0") == "1"

def set_test_mode(enabled: bool) -> None:
    global IN_TEST_MODE
    IN_TEST_MODE = enabled

def in_test_mode() -> bool:
    return IN_TEST_MODE

