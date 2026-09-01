import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from drift.host_facts import (
    get_host_os,
    get_host_arch,
    parse_os_release,
    get_host_distro,
    get_host_hostname,
    get_host_user,
    get_system_facts,
)


class TestHostFacts(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

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

    def test_get_system_facts(self) -> None:
        facts = get_system_facts()
        self.assertIn("drift_os", facts)
        self.assertIn("drift_arch", facts)
        self.assertIn("drift_distro", facts)
        self.assertIn("drift_hostname", facts)
        self.assertIn("drift_user", facts)
        self.assertTrue(all(isinstance(v, str) for v in facts.values()))


if __name__ == "__main__":
    unittest.main()
