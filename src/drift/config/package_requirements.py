"""Package host platform and environment requirements.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: Declarative Requirements Model & Matchers
    PackageRequirements (Dataclass)
        - check_requirements(): Evaluates declarative checks against host facts & environment
        - from_dict(): Factory parser from [package.requirements] dictionary
    match_ip_address(pattern, ip): Evaluates single IP against exact, CIDR, or wildcard
    match_ip_addresses(patterns, host_ips): Evaluates multiple host IPs against patterns
===============================================================================
"""

import ipaddress
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, List, Optional, Tuple

from ..core.exceptions import ConfigError
from ..utils.toml_utils import get_first_from, validate_known_keys


def match_ip_address(pattern: str, ip: str) -> bool:
    """Matches a single IP address against an exact IP, CIDR subnet, or wildcard pattern."""
    # Exact match
    if pattern == ip:
        return True
    # Wildcard match (e.g. 192.168.1.* or 10.0.*)
    if "*" in pattern:
        prefix = pattern.split("*")[0]
        if ip.startswith(prefix):
            return True
    # CIDR subnet match (e.g. 192.168.1.0/24 or 10.0.0.0/8)
    if "/" in pattern:
        try:
            net = ipaddress.ip_network(pattern, strict=False)
            addr = ipaddress.ip_address(ip)
            if addr in net:
                return True
        except (ValueError, TypeError):
            pass
    return False


def match_ip_addresses(patterns: Iterable[str], host_ips: Iterable[str]) -> bool:
    """Returns True if any host IP matches any of the given IP patterns."""
    resolved_host_ips = tuple(host_ips)
    return any(
        match_ip_address(p, ip)
        for p in patterns
        for ip in resolved_host_ips
    )


@dataclass
class PackageRequirements:
    """Declarative host platform and environment requirements for a package."""
    IP_KEYS: ClassVar[Tuple[str, ...]] = ("ip", "ips", "ip_addresses")
    KNOWN_KEYS: ClassVar[Tuple[str, ...]] = (
        "os",
        "arch",
        "distro",
        "binaries",
        "env",
        *IP_KEYS,
    )

    os: List[str] = field(default_factory=list)
    arch: List[str] = field(default_factory=list)
    distro: List[str] = field(default_factory=list)
    binaries: List[str] = field(default_factory=list)
    env: List[str] = field(default_factory=list)
    ip: List[str] = field(default_factory=list)

    def check_requirements(self) -> Tuple[bool, Optional[str]]:
        """Evaluates declarative requirements against host facts and environment.

        Returns:
            Tuple of (is_satisfied: bool, failure_reason: Optional[str]).
        """
        from ..utils.host_facts import get_host_os, get_host_arch, get_host_distro

        # 1. Check OS
        if self.os:
            current_os = os.environ.get("drift_os") or get_host_os()
            if current_os not in self.os:
                return False, f"Host OS '{current_os}' not in required list: {self.os}"

        # 2. Check Architecture
        if self.arch:
            current_arch = os.environ.get("drift_arch") or get_host_arch()
            if current_arch not in self.arch:
                return False, f"Host architecture '{current_arch}' not in required list: {self.arch}"

        # 3. Check Linux Distro
        if self.distro:
            current_distro = os.environ.get("drift_distro") or get_host_distro()
            if current_distro not in self.distro:
                return False, f"Linux distribution '{current_distro}' not in required list: {self.distro}"

        # 4. Check Binaries in PATH
        for binary in self.binaries:
            if not shutil.which(binary):
                return False, f"Required binary '{binary}' not found in PATH"

        # 5. Check Environment Variables
        for env_var in self.env:
            if not os.environ.get(env_var):
                return False, f"Required environment variable '{env_var}' is unset or empty"

        # 6. Check Host LAN IP addresses
        if self.ip:
            raw_ips = os.environ.get("drift_ip_addresses")
            if raw_ips is not None:
                host_ips = [ip.strip() for ip in raw_ips.split(";") if ip.strip()]
            else:
                from ..utils.host_facts import get_host_ip_addresses
                host_ips = get_host_ip_addresses()

            if not match_ip_addresses(self.ip, host_ips):
                return False, f"Host IP addresses {host_ips} do not match any required IP pattern: {self.ip}"

        return True, None

    @classmethod
    def from_dict(cls, data: Any, package_name: str = "") -> "PackageRequirements":
        """Parses and validates a PackageRequirements instance from a dictionary."""
        if not data:
            return cls()
        if not isinstance(data, dict):
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"[package.requirements] must be a table{name_str}.")

        name_str = f" for package '{package_name}'" if package_name else ""
        validate_known_keys(data, cls.KNOWN_KEYS, context="requirements", suffix=name_str)

        def _to_list_str(val: Any, field_name: str) -> List[str]:
            if val is None:
                return []
            if isinstance(val, str):
                s = val.strip()
                return [s] if s else []
            if isinstance(val, (list, tuple)):
                res = []
                for item in val:
                    if not isinstance(item, str):
                        name_str = f" for package '{package_name}'" if package_name else ""
                        raise ConfigError(f"Items in '{field_name}' must be strings{name_str}.")
                    s = item.strip()
                    if s:
                        res.append(s)
                return res
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"'{field_name}' under requirements must be a string or list of strings{name_str}.")

        raw_ip = get_first_from(data, cls.IP_KEYS)

        return cls(
            os=_to_list_str(data.get("os"), "os"),
            arch=_to_list_str(data.get("arch"), "arch"),
            distro=_to_list_str(data.get("distro"), "distro"),
            binaries=_to_list_str(data.get("binaries"), "binaries"),
            env=_to_list_str(data.get("env"), "env"),
            ip=_to_list_str(raw_ip, "ip"),
        )
