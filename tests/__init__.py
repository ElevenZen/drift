import os
import sys
import shutil
from pathlib import Path
from drift.core.constants import set_test_mode

# On Windows, ensure test runner PATH includes functional Git Bash if available
if sys.platform == "win32":
    current_bash = shutil.which("bash") or shutil.which("bash.exe")
    if not current_bash or "system32" in current_bash.lower():
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        for base in [program_files, program_files_x86, os.path.join(local_app_data, "Programs") if local_app_data else None]:
            if not base:
                continue
            git_bin = Path(base) / "Git" / "bin"
            if (git_bin / "bash.exe").is_file():
                git_usr_bin = Path(base) / "Git" / "usr" / "bin"
                os.environ["PATH"] = f"{git_bin}{os.pathsep}{git_usr_bin}{os.pathsep}{os.environ.get('PATH', '')}"
                break

# Disable interactive pagers and editors during tests to prevent blocking and pop-up windows.
os.environ["PAGER"] = "cat"
os.environ["GIT_PAGER"] = "cat"
os.environ["DRIFT_TEST_MODE"] = "1"
os.environ.pop("EDITOR", None)
os.environ.pop("VISUAL", None)

# Enable test mode with logging disabled by default
set_test_mode(True, enable_logging=False)
