import os
# Disable interactive pagers during tests to prevent blocking and pop-up windows.
os.environ["PAGER"] = "cat"
os.environ["GIT_PAGER"] = "cat"
