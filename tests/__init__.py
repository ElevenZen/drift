import os
from drift.constants import set_test_mode

# Disable interactive pagers during tests to prevent blocking and pop-up windows.
os.environ["PAGER"] = "cat"
os.environ["GIT_PAGER"] = "cat"
os.environ["DRIFT_TEST_MODE"] = "1"

# Enable test mode with logging disabled by default
set_test_mode(True, enable_logging=False)
