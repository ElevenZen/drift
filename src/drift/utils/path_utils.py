"""Pure path helpers for path expansion, dot-prefix encoding, and relative path computation.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

All functions in this module are pure (no filesystem I/O except Path.home()
for tilde expansion). They operate on path representations only.

    expand_path(path_input) — Expands ~, $VAR, %VAR% in path strings.
    is_relative_to(path, other) — Polyfill for Path.is_relative_to (Python <3.9).
    resolve_target_path(relative_path, relative_base) — Applies dot-prefix + env expansion.
    encode_dot_prefix(relative_path) — Converts 'dot-config' → '.config' in path segments.
    decode_dot_prefix(relative_path) — Converts '.config' → 'dot-config' in path segments.
    relative_path_between(from_dir, to_path) — Computes relative path between two absolute paths.

===============================================================================
"""

import os
import sys
import re
from pathlib import Path
from typing import Union


COMMON_WINDOWS_PATH_ENVS = {
    "USERPROFILE": lambda: str(Path.home()),
    "APPDATA": lambda: str(Path.home() / "AppData" / "Roaming"),
    "LOCALAPPDATA": lambda: str(Path.home() / "AppData" / "Local"),
    "PROGRAMDATA": lambda: r"C:\ProgramData",
    "HOMEDRIVE": lambda: Path.home().drive or "C:",
    "HOMEPATH": lambda: str(Path.home().relative_to(Path.home().anchor)) if Path.home().drive else str(Path.home()),
    "TEMP": lambda: os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home() / "AppData" / "Local" / "Temp"),
    "TMP": lambda: os.environ.get("TMP") or os.environ.get("TEMP") or str(Path.home() / "AppData" / "Local" / "Temp"),
    "SYSTEMROOT": lambda: os.environ.get("SYSTEMROOT") or r"C:\Windows",
    "WINDIR": lambda: os.environ.get("WINDIR") or r"C:\Windows",
    "ALLUSERSPROFILE": lambda: os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData",
    "PROGRAMFILES": lambda: os.environ.get("PROGRAMFILES") or r"C:\Program Files",
    "PROGRAMFILES(X86)": lambda: os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)",
}


def expand_path(path_input: Union[str, Path]) -> Path:
    """Expands '~', Windows-style '%VAR%', and standard '$VAR' in path inputs."""
    raw = str(path_input).strip()
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
    raw = os.path.expandvars(raw)

    # 3. Expand ~ or ~/ or ~\ at beginning of path cleanly across platforms
    if raw == "~":
        return Path.home()
    if raw.startswith("~/") or raw.startswith("~\\"):
        subpath = raw[2:].lstrip("/\\")
        parts = [p for p in re.split(r"[/\\]+", subpath) if p]
        return Path.home().joinpath(*parts)

    # 4. Normalize backslashes to forward slashes for cross-platform consistency
    if sys.platform == "win32" and "\\" in raw:
        raw = raw.replace("\\", "/")

    # 5. Final expanduser call to ensure clean Path conversion
    return Path(raw).expanduser()


def is_relative_to(path: Path, other: Path) -> bool:
    """Robust fallback implementation of Path.is_relative_to for Python < 3.9."""
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def resolve_target_path(relative_path: Path, relative_base: Path) -> Path:
    """Applies dot-prefix translation and env expansion to compose a target path."""
    translated_path = encode_dot_prefix(relative_path)
    target_path = expand_path(relative_base)
    return target_path / translated_path


def encode_dot_prefix(relative_path: Path) -> Path:
    """Converts 'dot-' prefixes in path segments to leading dots ('.').

    Does not translate 'dot-' or 'dot-.' segments (only segments with suffix after 'dot-').
    """
    translated_parts = ["." + p[4:]
                        if p.startswith("dot-") and p not in ("dot-", "dot-.") else p
                        for p in relative_path.parts]
    assert len(translated_parts) == len(relative_path.parts), "Translation should not change the number of path segments."
    return Path(*translated_parts)


def decode_dot_prefix(relative_path: Path) -> Path:
    """Converts leading dots ('.') in path segments to 'dot-', the inverse of encode_dot_prefix().

    Does not translate '.' and '..' segments.
    """
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
