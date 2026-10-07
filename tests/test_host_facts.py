import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from drift.utils.host_facts import (
    get_host_os,
    get_host_arch,
    parse_os_release,
    get_host_distro,
    get_host_hostname,
    get_host_user,
    init_system_facts,
    get_cached_system_facts,
    SYSTEM_FACTS,
)
from drift.core.constants import set_test_mode


class TestHostFacts(unittest.TestCase):
    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_get_host_os(self) -> None:
        with patch("sys.platform", "linux"):
            self.assertEqual(get_host_os(), "linux")
        with patch("sys.platform", "darwin"):
            self.assertEqual(get_host_os(), "darwin")
        with patch("sys.platform", "win32"):
            self.assertEqual(get_host_os(), "windows")
        with patch("sys.platform", "freebsd13"):
            self.assertEqual(get_host_os(), "freebsd")

    def test_get_host_arch(self) -> None:
        with patch("platform.machine", return_value="x86_64"):
            self.assertEqual(get_host_arch(), "x86_64")
        with patch("platform.machine", return_value="AMD64"):
            self.assertEqual(get_host_arch(), "x86_64")
        with patch("platform.machine", return_value="arm64"), patch("sys.platform", "darwin"):
            self.assertEqual(get_host_arch(), "arm64")
        with patch("platform.machine", return_value="aarch64"), patch("sys.platform", "linux"):
            self.assertEqual(get_host_arch(), "aarch64")

    def test_parse_os_release(self) -> None:
        os_release = self.root / "os-release"
        os_release.write_text("""
# This is a comment
NAME="Ubuntu"
VERSION="22.04.1 LTS (Jammy Jellyfish)"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 22.04.1 LTS"
""", encoding="utf-8")
        facts = parse_os_release(os_release)
        self.assertEqual(facts.get("NAME"), "Ubuntu")
        self.assertEqual(facts.get("ID"), "ubuntu")
        self.assertEqual(facts.get("ID_LIKE"), "debian")

        # Test explicit keyword argument
        facts_kw = parse_os_release(os_release_path_override=os_release)
        self.assertEqual(facts_kw.get("ID"), "ubuntu")

    def test_get_host_distro(self) -> None:
        # Non-Linux
        with patch("sys.platform", "darwin"):
            self.assertEqual(get_host_distro(), "macos")
        with patch("sys.platform", "win32"):
            self.assertEqual(get_host_distro(), "windows")
        with patch("sys.platform", "freebsd13"):
            self.assertEqual(get_host_distro(), "freebsd")

        # Linux Ubuntu
        ubuntu_rel = self.root / "ubuntu-release"
        ubuntu_rel.write_text('ID=ubuntu\nID_LIKE=debian\n', encoding="utf-8")
        with patch("sys.platform", "linux"):
            self.assertEqual(get_host_distro(ubuntu_rel), "ubuntu")

        # Linux Arch
        arch_rel = self.root / "arch-release"
        arch_rel.write_text('ID="arch"\n', encoding="utf-8")
        with patch("sys.platform", "linux"):
            self.assertEqual(get_host_distro(arch_rel), "arch")
            self.assertEqual(get_host_distro(os_release_path_override=arch_rel), "arch")

        # Linux Fallback
        empty_rel = self.root / "empty-release"
        empty_rel.write_text('# nothing\n', encoding="utf-8")
        with patch("sys.platform", "linux"):
            self.assertEqual(get_host_distro(empty_rel), "linux")

    def test_get_host_hostname_and_user(self) -> None:
        with patch("socket.gethostname", return_value="my-laptop.local"):
            self.assertEqual(get_host_hostname(), "my-laptop")
        with patch("getpass.getuser", return_value="alice"):
            self.assertEqual(get_host_user(), "alice")

    def test_get_cached_system_facts(self) -> None:
        facts = get_cached_system_facts()
        self.assertIn("drift_os", facts)
        self.assertIn("drift_arch", facts)
        self.assertIn("drift_distro", facts)
        self.assertIn("drift_hostname", facts)
        self.assertIn("drift_user", facts)
        self.assertIn("drift_ip_addresses", facts)
        self.assertTrue(all(isinstance(v, str) for v in facts.values()))

        # Test with custom os-release override via init_system_facts
        custom_rel = self.root / "custom-release"
        custom_rel.write_text('ID=fedora\n', encoding="utf-8")
        with patch("sys.platform", "linux"):
            facts_custom = init_system_facts(os_release_path_override=custom_rel)
            self.assertEqual(facts_custom["drift_distro"], "fedora")
            self.assertEqual(get_cached_system_facts()["drift_distro"], "fedora")

    def test_get_host_ip_addresses_enumerates_interfaces(self) -> None:
        from drift.utils.host_facts import get_host_ip_addresses
        ips = get_host_ip_addresses()
        self.assertIsInstance(ips, list)
        for ip in ips:
            self.assertIsInstance(ip, str)
            self.assertFalse(ip.startswith("127."))
            # Verify valid IPv4 string format
            parts = ip.split(".")
            self.assertEqual(len(parts), 4)
            for part in parts:
                self.assertTrue(0 <= int(part) <= 255)

    def test_init_system_facts_populates_global_without_mutating_os_environ(self) -> None:
        """Verifies init_system_facts caches facts in SYSTEM_FACTS without altering os.environ."""
        import drift.utils.host_facts as hf
        # Clear any existing drift_* in os.environ
        drift_keys = ["drift_os", "drift_arch", "drift_distro", "drift_hostname", "drift_user", "drift_ip_addresses"]
        for k in drift_keys:
            os.environ.pop(k, None)

        hf.SYSTEM_FACTS.clear()
        facts = init_system_facts()
        self.assertIs(facts, hf.SYSTEM_FACTS)
        self.assertIn("drift_os", facts)
        self.assertIn("drift_arch", facts)
        self.assertIn("drift_distro", facts)
        self.assertIn("drift_hostname", facts)
        self.assertIn("drift_user", facts)
        self.assertIn("drift_ip_addresses", facts)

        # Invariant: os.environ must NOT be mutated!
        for k in drift_keys:
            self.assertNotIn(k, os.environ)

    def test_get_cached_system_facts_lazy_initialization(self) -> None:
        """Verifies get_cached_system_facts lazily initializes SYSTEM_FACTS if empty."""
        import drift.utils.host_facts as hf
        hf.SYSTEM_FACTS.clear()
        cached = get_cached_system_facts()
        self.assertIs(cached, hf.SYSTEM_FACTS)
        self.assertIn("drift_os", cached)
        self.assertTrue(len(cached) >= 6)


if __name__ == "__main__":
    unittest.main()
