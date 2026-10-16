import os
import sys
import shutil
from pathlib import Path
from drift.core.constants import set_test_mode

def _patch_windows_git_bash_for_tests() -> None:
    """Test patch for Windows CI / test environments lacking WSL or bash in PATH.

    Prepends Git Bash directories to PATH so that test subprocesses and test runners
    can execute bash scripts and unix utilities during test discovery without requiring WSL.
    """
    if sys.platform != "win32":
        return

    git_exe = shutil.which("git") or shutil.which("git.exe")
    git_roots = []
    if git_exe:
        git_roots.append(Path(git_exe).resolve().parent.parent)

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")

    candidate_roots = [
        Path(program_files) / "Git",
        Path(program_files_x86) / "Git",
        Path(local_app_data) / "Programs" / "Git" if local_app_data else None,
        Path(user_profile) / "scoop" / "apps" / "git" / "current" if user_profile else None,
        Path(r"C:\ProgramData\chocolatey"),
    ]
    git_roots.extend([r for r in candidate_roots if r and r.is_dir()])

    paths_to_prepend = []
    for root in git_roots:
        bin_dir = root / "bin"
        usr_bin_dir = root / "usr" / "bin"
        if (bin_dir / "bash.exe").is_file():
            paths_to_prepend.append(str(bin_dir))
        if (usr_bin_dir / "bash.exe").is_file():
            paths_to_prepend.append(str(usr_bin_dir))
        if paths_to_prepend:
            break

    if paths_to_prepend:
        current_path = os.environ.get("PATH", "")
        current_lower = current_path.lower()
        new_paths = [p for p in paths_to_prepend if p.lower() not in current_lower]
        if new_paths:
            os.environ["PATH"] = f"{os.pathsep.join(new_paths)}{os.pathsep}{current_path}"


def _patch_no_symlink_for_tests() -> None:
    """Simulates an unprivileged environment without symlink creation privileges
    when DRIFT_TEST_NO_SYMLINK=1 is set in the environment.
    """
    if os.environ.get("DRIFT_TEST_NO_SYMLINK") != "1":
        return

    def _disabled_symlink(*args, **kwargs) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    os.symlink = _disabled_symlink
    Path.symlink_to = _disabled_symlink  # type: ignore[method-assign]
    from drift.utils.file_inspect import check_can_symlink
    check_can_symlink.cache_clear()


_patch_windows_git_bash_for_tests()
_patch_no_symlink_for_tests()

# Disable interactive pagers and editors during tests to prevent blocking and pop-up windows.
os.environ["PAGER"] = "cat"
os.environ["GIT_PAGER"] = "cat"
os.environ["drift_test_mode"] = "1"
os.environ["DRIFT_TEST_MODE"] = "1"
os.environ.setdefault("USER", os.environ.get("USERNAME", "test_user"))
os.environ.setdefault("GIT_AUTHOR_NAME", "Drift Test")
os.environ.setdefault("GIT_COMMITTER_NAME", "Drift Test")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@drift.local")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@drift.local")
os.environ.pop("EDITOR", None)
os.environ.pop("VISUAL", None)

# Enable test mode with logging disabled by default
set_test_mode(True, enable_logging=False)

