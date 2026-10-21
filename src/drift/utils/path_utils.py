"""Pure path helpers for path expansion, dot-prefix encoding, and relative path computation.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

All functions in this module are pure (no filesystem I/O except Path.home()
for tilde expansion). They operate on path representations only.

    as_posix(path) — Converts Path, PurePath, or str into a POSIX path string.
    as_windows(path) — Converts Path, PurePath, or str into a Windows backslash path string.
    expand_path(path_input) — Expands ~, $VAR, %VAR% in path strings.
    check_is_relative_path(path) — True iff path has no drive or root anchor (State 1).
    check_is_partially_anchored_path(path) — True iff path has an anchor but is not absolute (State 2 on Windows).
    is_relative_to(path, other) — Polyfill for Path.is_relative_to (Python <3.9).
    encode_dot_prefix(relative_path) — Converts 'dot-config' → '.config' in path segments.
    decode_dot_prefix(relative_path) — Converts '.config' → 'dot-config' in path segments.
    relative_path_between(from_dir, to_path) — Computes relative path between two absolute paths.
    compute_relative_symlink_target(source_path, link_parent_dir) — Robust symlink target with cross-drive fallback.
    rebase_path(path, old_base, new_base) — Pure path rebasing from old_base to new_base prefix.

===============================================================================
"""

import os
import posixpath
import sys
import re
from pathlib import Path, PurePath, PureWindowsPath
from typing import Union, Optional


COMMON_WINDOWS_PATH_ENVS = {
    "USERPROFILE": lambda: rf"{Path.home()}",
    "APPDATA": lambda: rf"{Path.home()}\AppData\Roaming",
    "LOCALAPPDATA": lambda: rf"{Path.home()}\AppData\Local",
    "PROGRAMDATA": lambda: r"C:\ProgramData",
    "HOMEDRIVE": lambda: Path.home().drive or "C:",
    "HOMEPATH": lambda: (
        rf"\{Path.home().relative_to(Path.home().anchor)}"
        if Path.home().drive
        else rf"{Path.home()}"
    ),
    "TEMP": lambda: rf"{Path.home()}\AppData\Local\Temp",
    "TMP": lambda: rf"{Path.home()}\AppData\Local\Temp",
    "SYSTEMROOT": lambda: r"C:\Windows",
    "WINDIR": lambda: r"C:\Windows",
    "ALLUSERSPROFILE": lambda: r"C:\ProgramData",
    "PROGRAMFILES": lambda: r"C:\Program Files",
    "PROGRAMFILES(X86)": lambda: r"C:\Program Files (x86)",
}


def as_posix(path: Union[str, PurePath]) -> str:
    """Converts a Path, PurePath, or string into a POSIX path string."""
    if isinstance(path, PurePath):
        return path.as_posix()
    return str(path)


def as_windows(path: Union[str, PurePath]) -> str:
    """Converts a Path, PurePath, or string into a Windows backslash path string."""
    return os.fspath(PureWindowsPath(as_posix(path)))


def expand_path(path_input: Union[str, Path]) -> Path:
    """Expands '~', Windows-style '%VAR%', and standard '$VAR' in path inputs."""
    raw = as_posix(path_input).strip()
    if not raw:
        return Path(".")

    # 1. Expand %VAR% syntax ONLY on Windows
    if sys.platform == "win32" and "%" in raw:
        def replace_win_env(match: re.Match) -> str:
            var_name = match.group(1)
            var_upper = var_name.upper()
            if var_name in os.environ:
                return os.environ[var_name]
            if var_upper in os.environ:
                return os.environ[var_upper]
            if var_upper in COMMON_WINDOWS_PATH_ENVS:
                return COMMON_WINDOWS_PATH_ENVS[var_upper]()
            return f"%{var_name}%"

        raw = re.sub(r"%([A-Za-z0-9_()]+)%", replace_win_env, raw)

    # 2. Expand $VAR / ${VAR} syntax
    raw = posixpath.expandvars(raw)

    # 3. Normalize backslashes to forward slashes for cross-platform consistency
    if sys.platform == "win32" and "\\" in raw:
        raw = raw.replace("\\", "/")

    # 4. Expand ~ or ~/ at beginning of path cleanly across platforms
    if raw == "~":
        return Path.home()
    if raw.startswith("~/"):
        subpath = raw[2:].lstrip("/")
        parts = [p for p in subpath.split("/") if p]
        return Path.home().joinpath(*parts)

    # 5. Final expanduser call to ensure clean Path conversion
    return Path(raw).expanduser()


def check_is_relative_path(path: Path) -> bool:
    """Returns True if path is strictly relative (State 1: has neither drive nor root anchor)."""
    return not path.anchor


def check_is_partially_anchored_path(path: Path) -> bool:
    """Returns True if path has a drive or root anchor without being a full absolute path (State 2 on Windows)."""
    return bool(path.anchor) and not path.is_absolute()


def is_relative_to(path: Path, other: Path) -> bool:
    """Robust fallback implementation of Path.is_relative_to for Python < 3.9."""
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def encode_dot_prefix(relative_path: Path) -> Path:
    """Converts 'dot-' prefixes in path segments to leading dots ('.').

    Does not translate 'dot-' or 'dot-.' segments (only segments with suffix after 'dot-').
    """
    assert not relative_path.anchor, f"encode_dot_prefix expects a strictly relative path without anchor, got '{relative_path}'."
    translated_parts = ["." + p[4:]
                        if p.startswith("dot-") and p not in ("dot-", "dot-.") else p
                        for p in relative_path.parts]
    assert len(translated_parts) == len(relative_path.parts), "Translation should not change the number of path segments."
    return Path(*translated_parts)


def decode_dot_prefix(relative_path: Path) -> Path:
    """Converts leading dots ('.') in path segments to 'dot-', the inverse of encode_dot_prefix().

    Does not translate '.' and '..' segments.
    """
    assert not relative_path.anchor, f"decode_dot_prefix expects a strictly relative path without anchor, got '{relative_path}'."
    translated_parts = ["dot-" + p[1:]
                        if p.startswith(".") and p not in (".", "..") else p
                        for p in relative_path.parts]
    assert len(translated_parts) == len(relative_path.parts), "Reverse translation should not change the number of path segments."
    return Path(*translated_parts)


def relative_path_between(from_dir: Path, to_path: Path) -> Path:
    """Computes the relative path from from_dir to to_path using only Path objects."""
    abs_from = from_dir.resolve()
    abs_to = to_path.resolve()

    from_parts = abs_from.parts
    to_parts = abs_to.parts

    common_idx = 0
    while common_idx < len(from_parts) and common_idx < len(to_parts) and from_parts[common_idx] == to_parts[common_idx]:
        common_idx += 1

    ups = [".."] * (len(from_parts) - common_idx)
    downs = list(to_parts[common_idx:])

    return Path(*ups).joinpath(*downs)


def compute_relative_symlink_target(source_path: Path, link_parent_dir: Path) -> Path:
    """Computes the relative symlink target path pointing from link_parent_dir to source_path.

    Resolves link_parent_dir to its physical path to handle external directory symlinks safely.
    If source and destination reside on distinct drives/roots where a relative path
    cannot be constructed (e.g. cross-drive on Windows), gracefully falls back to an
    absolute canonical path.
    """
    resolved_parent = link_parent_dir.resolve()
    resolved_source = source_path.resolve()

    # Cross-drive on Windows (e.g. C: vs D:) cannot be expressed relatively
    if resolved_parent.drive != resolved_source.drive:
        return resolved_source

    try:
        rel = relative_path_between(resolved_parent, resolved_source)
        if rel == Path():
            return resolved_source
        return rel
    except Exception:
        return resolved_source


def to_relative_path(path: Path, base_dir: Path) -> Path:
    """Returns a relative Path if path is relative to base_dir, else the path itself."""
    return path.relative_to(base_dir) if is_relative_to(path, base_dir) else path


def to_relative_posix(path: Path, base_dir: Path) -> str:
    """Returns a relative POSIX path string if path is relative to base_dir, else the path as POSIX."""
    return to_relative_path(path, base_dir).as_posix()


def rebase_path(path: Optional[Path], old_base: Path, new_base: Path) -> Optional[Path]:
    """Rebases a path from an old directory prefix to a new directory prefix.

    If path is None or does not share old_base as prefix, returns path unmodified.
    """
    if path is None:
        return None
    try:
        rel = path.relative_to(old_base)
        return new_base / rel
    except ValueError:
        return path




