"""Global constants for the drift dotfiles manager."""

CONFIG_DIR_NAME = "config"
GLOBAL_CONFIG_FILE_NAME = "drift.toml"
PACKAGE_CONFIG_FILE_NAME_LIST = ["drift_package.toml", "package.toml"]

import os
IN_TEST_MODE: bool = os.environ.get("DRIFT_TEST_MODE", "0") == "1"

def set_test_mode(enabled: bool) -> None:
    global IN_TEST_MODE
    IN_TEST_MODE = enabled

def in_test_mode() -> bool:
    return IN_TEST_MODE

