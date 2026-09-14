import os
from drift.constants import set_test_mode

# Disable interactive pagers and editors during tests to prevent blocking and pop-up windows.
os.environ["PAGER"] = "cat"
os.environ["GIT_PAGER"] = "cat"
os.environ["DRIFT_TEST_MODE"] = "1"
os.environ.pop("EDITOR", None)
os.environ.pop("VISUAL", None)

# Enable test mode with logging disabled by default
set_test_mode(True, enable_logging=False)
