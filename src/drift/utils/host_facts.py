"""Automated zero-dependency host facts detection and system profiling.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Public Fact Ingestion Entry Point
    get_system_facts(os_release_path_override)
        SystemFacts.probe(os_release_path_override) [Layer 2]
        SystemFacts.to_envs(ip_separator=";") [Layer 2]

Layer 2: Structured Data Model & System Facts Aggregator
    SystemFacts (dataclass)
        .probe(os_release_path_override)
            get_host_os [Layer 1]
            get_host_arch [Layer 1]
            get_host_distro [Layer 1] (parse_os_release)
            get_host_hostname [Layer 1]
            get_host_user [Layer 1]
            get_host_ip_addresses [Layer 1] (_get_ips_from_getifaddrs / _get_ips_from_windows / UDP probes)
        .to_envs(ip_separator=";")
            Maps probed facts into standardized drift_* environment variables:
            drift_os, drift_arch, drift_distro, drift_hostname, drift_user, drift_ip_addresses

Layer 1: Low-Level OS, Hardware & Network Probing Primitives
    get_host_os: Normalizes platform (linux, darwin, windows, freebsd).
    get_host_arch: Normalizes CPU architecture (x86_64, arm64, x86).
    parse_os_release: Parses /etc/os-release or /usr/lib/os-release key-value pairs.
    get_host_distro: Normalizes Linux/BSD distro (ubuntu, arch, debian, etc.) or OS name.
    get_host_hostname: Retrieves local hostname without FQDN suffix.
    get_host_user: Retrieves current login username.
    _is_useful_ip: Filters out loopback and link-local addresses (IPv4 and IPv6).
    _get_ips_from_getifaddrs: Direct POSIX libc network interface enumeration via ctypes (IPv4 + IPv6).
    _get_ips_from_windows: Windows adapter IP resolution via socket APIs (IPv4 + IPv6).
    get_host_ip_addresses: Aggregates non-loopback IPv4/IPv6 addresses with UDP routing fallback.

===============================================================================
"""

import os
import sys
import platform
import socket
import getpass
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, List


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


def parse_os_release(os_release_path_override: Optional[Path] = None) -> Dict[str, str]:
    """Parses standard Freedesktop /etc/os-release or /usr/lib/os-release key-value pairs."""
    from .env_utils import parse_env_file

    paths_to_check = ([os_release_path_override]
                      if os_release_path_override
                      else [Path("/etc/os-release"), Path("/usr/lib/os-release")])
    for p in paths_to_check:
        if not p or not p.is_file():
            continue
        facts = parse_env_file(p)
        if facts:
            return facts
    return {}


def get_host_distro(os_release_path_override: Optional[Path] = None) -> str:
    """Returns the normalized OS distribution identifier (e.g. 'ubuntu', 'arch', 'debian', 'macos', 'windows')."""
    os_name = get_host_os()
    if os_name == "darwin":
        return "macos"
    elif os_name == "windows":
        return "windows"
    elif os_name == "freebsd":
        return "freebsd"

    # On Linux / POSIX, check os-release
    facts = parse_os_release(os_release_path_override)
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


def _is_useful_ip(ip_str: str) -> bool:
    """Returns True if the IP address is not loopback or link-local (IPv4 and IPv6)."""
    return (
        not ip_str.startswith("127.")      # IPv4 loopback
        and ip_str != "::1"                # IPv6 loopback
        and not ip_str.startswith("fe80:")  # IPv6 link-local
    )


def _get_ips_from_getifaddrs() -> List[str]:
    """Enumerates all network interface IPs using POSIX libc getifaddrs (macOS, Linux, FreeBSD).

    Collects both IPv4 (AF_INET) and IPv6 (AF_INET6) addresses, filtering out
    loopback and link-local addresses.
    """
    try:
        import ctypes
        import ctypes.util

        libc_name = ctypes.util.find_library("c") or "libc.so"
        libc = ctypes.CDLL(libc_name)

        # Define the ifaddrs structure for storing the result of getifaddrs()
        class ifaddrs(ctypes.Structure):
            pass

        ifaddrs._fields_ = [
            ("ifa_next", ctypes.POINTER(ifaddrs)),
            ("ifa_name", ctypes.c_char_p),
            ("ifa_flags", ctypes.c_uint),
            ("ifa_addr", ctypes.c_void_p),
            ("ifa_netmask", ctypes.c_void_p),
            ("ifa_dstaddr", ctypes.c_void_p),
            ("ifa_data", ctypes.c_void_p),
        ]

        addrs = ctypes.POINTER(ifaddrs)()
        if libc.getifaddrs(ctypes.byref(addrs)) != 0:
            return []

        ips: List[str] = []
        curr = addrs
        while curr:
            ifa = curr.contents
            if ifa.ifa_addr:
                addr_ptr = ifa.ifa_addr
                # Detect address family from sockaddr header.
                # On BSD/macOS sa_family is at byte offset 1 (uint8); on Linux at offset 0 (uint16).
                # See 'struct sockaddr' layout for each platform.
                if sys.platform == "darwin" or sys.platform.startswith("freebsd"):
                    family = ctypes.c_uint8.from_address(addr_ptr + 1).value
                else:
                    family = ctypes.c_uint16.from_address(addr_ptr).value

                ip_str = None
                if family == socket.AF_INET:
                    # sockaddr_in: address at offset 4, 4 bytes
                    raw_ip = ctypes.string_at(addr_ptr + 4, 4)
                    ip_str = socket.inet_ntoa(raw_ip)
                elif family == socket.AF_INET6:
                    # sockaddr_in6: address at offset 8 (after family+port+flowinfo), 16 bytes
                    raw_ip = ctypes.string_at(addr_ptr + 8, 16)
                    ip_str = socket.inet_ntop(socket.AF_INET6, raw_ip)

                if ip_str and _is_useful_ip(ip_str) and ip_str not in ips:
                    ips.append(ip_str)
            curr = ifa.ifa_next

        libc.freeifaddrs(addrs)
        return ips
    except Exception:
        return []


def _get_ips_from_windows() -> List[str]:
    """Enumerates adapter IP addresses on Windows via getaddrinfo (IPv4 + IPv6)."""
    ips: List[str] = []
    try:
        hostname = socket.gethostname()
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
        for info in infos:
            ip: str = str(info[4][0])
            if ip and _is_useful_ip(ip) and ip not in ips:
                ips.append(ip)
    except (socket.error, OSError):
        pass
    return ips


def get_host_ip_addresses() -> List[str]:
    """Returns a list of local non-loopback IP addresses (IPv4 and IPv6) across network interfaces.

    Primary discovery uses kernel interface enumeration (libc `getifaddrs` on POSIX,
    `getaddrinfo` on Windows). Supplementary connectionless UDP routing table probes
    are then performed to discover virtual/VPN adapter IPs on Windows and serve as a
    zero-dependency fallback if interface enumeration fails.

    Note: UDP SOCK_DGRAM connect() does NOT send any packet — it is a purely local
    kernel routing table query that reveals which source address the OS would use to
    reach a given destination. All probes are safe and invisible to the network.

    Returns:
        A deduplicated list of non-loopback, non-link-local IP address strings (IPv4 and IPv6).
    """
    ips: List[str] = []

    # 1. Direct kernel network interface enumeration (macOS, Linux, BSD)
    if sys.platform != "win32":
        ips.extend(_get_ips_from_getifaddrs())
    else:
        ips.extend(_get_ips_from_windows())

    # 2. Supplementary UDP routing table probes (no packets sent).
    # On POSIX where getifaddrs() succeeds, all interface IPs are already collected,
    # making this probe redundant for IP discovery. However, this probe serves two purposes:
    #   a) Primary supplement on Windows, where getaddrinfo() may miss VPNs,
    #      Hyper-V/WSL virtual adapters, and secondary network interfaces.
    #   b) Zero-dependency fallback on POSIX systems where ctypes / libc getifaddrs() fails
    #      (e.g., restricted containers, sandboxes, or minimal Python runtimes).
    probe_destinations = [
        # IPv4: RFC1918 private subnets + default internet route
        (socket.AF_INET, "10.255.255.255", 1),
        (socket.AF_INET, "172.31.255.255", 1),
        (socket.AF_INET, "192.168.255.255", 1),
        (socket.AF_INET, "8.8.8.8", 80),
        # IPv6: ULA (fd00::/8) + default internet route (Google Public DNS)
        (socket.AF_INET6, "fd00::1", 1),
        (socket.AF_INET6, "2001:4860:4860::8888", 80),
    ]

    for family, dst_ip, port in probe_destinations:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((dst_ip, port))
                ip = s.getsockname()[0]
            if ip and _is_useful_ip(ip) and ip not in ips:
                ips.append(ip)
        except (socket.error, OSError):
            pass

    return ips


@dataclass
class SystemFacts:
    """Encapsulates auto-populated host system facts with type safety."""
    os: str = ""
    arch: str = ""
    distro: str = ""
    hostname: str = ""
    user: str = ""
    ip_addresses: List[str] = field(default_factory=list)

    @classmethod
    def probe(
        cls,
        os_release_path_override: Optional[Path] = None,
    ) -> "SystemFacts":
        """Probes the current host system facts."""
        return cls(
            os=get_host_os(),
            arch=get_host_arch(),
            distro=get_host_distro(os_release_path_override=os_release_path_override),
            hostname=get_host_hostname(),
            user=get_host_user(),
            ip_addresses=get_host_ip_addresses(),
        )

    def to_envs(self, ip_separator: str = ";") -> Dict[str, str]:
        """Converts system facts into a dictionary of drift_* environment variables."""
        return {
            "drift_os": self.os,
            "drift_arch": self.arch,
            "drift_distro": self.distro,
            "drift_hostname": self.hostname,
            "drift_user": self.user,
            "drift_ip_addresses": ip_separator.join(self.ip_addresses),
        }


def get_system_facts(
    os_release_path_override: Optional[Path] = None,
) -> Dict[str, str]:
    """Returns the dictionary of auto-populated lowercase drift host facts."""
    return SystemFacts.probe(os_release_path_override=os_release_path_override).to_envs()


def inject_system_facts() -> None:
    """Injects auto-populated host facts into os.environ if not already set in INITIAL_ENV."""
    from ..core.constants import INITIAL_ENV
    facts = get_system_facts()
    for k, v in facts.items():
        if k not in INITIAL_ENV:
            os.environ[k] = v
