"""Zero-dependency automated host facts detection module."""

import os
import sys
import platform
import socket
import getpass
from pathlib import Path
from typing import Dict, Optional


def get_host_os() -> str:
    """Returns the normalized operating system name."""
    if sys.platform == "win32":
        return "windows"
    elif sys.platform == "darwin":
        return "darwin"
    elif sys.platform.startswith("freebsd"):
        return "freebsd"
    elif sys.platform.startswith("linux"):
        return "linux"
    return platform.system().lower()


def get_host_arch() -> str:
    """Returns the normalized CPU architecture."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    elif machine in ("arm64", "aarch64"):
        return "arm64" if sys.platform == "darwin" else machine
    elif machine in ("i386", "i686", "x86"):
        return "x86"
    return machine


def parse_os_release(os_release_path: Optional[Path] = None) -> Dict[str, str]:
    """Parses standard Freedesktop /etc/os-release or /usr/lib/os-release key-value pairs."""
    from .env_utils import parse_env_file

    paths_to_check = ([os_release_path]
                      if os_release_path
                      else [Path("/etc/os-release"), Path("/usr/lib/os-release")])
    for p in paths_to_check:
        if not p or not p.is_file():
            continue
        facts = parse_env_file(p)
        if facts:
            return facts
    return {}


def get_host_distro(os_release_path: Optional[Path] = None) -> str:
    """Returns the normalized OS distribution identifier (e.g. 'ubuntu', 'arch', 'debian', 'macos', 'windows')."""
    os_name = get_host_os()
    if os_name == "darwin":
        return "macos"
    elif os_name == "windows":
        return "windows"
    elif os_name == "freebsd":
        return "freebsd"

    # On Linux / POSIX, check os-release
    facts = parse_os_release(os_release_path)
    distro_id = facts.get("ID", "").lower().strip()
    if distro_id:
        return distro_id

    # Fallback to ID_LIKE if ID is missing
    id_like = facts.get("ID_LIKE", "").lower().strip()
    if id_like:
        return id_like.split()[0]

    return "linux"


def get_host_hostname() -> str:
    """Returns the primary local hostname."""
    try:
        raw = socket.gethostname()
        return raw.split(".")[0].lower()
    except Exception:
        return "localhost"


def get_host_user() -> str:
    """Returns the current user login username."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))


def get_system_facts(os_release_path: Optional[Path] = None) -> Dict[str, str]:
    """Returns the dictionary of auto-populated lowercase drift host facts."""
    return {
        "drift_os": get_host_os(),
        "drift_arch": get_host_arch(),
        "drift_distro": get_host_distro(os_release_path=os_release_path),
        "drift_hostname": get_host_hostname(),
        "drift_user": get_host_user(),
    }
