"""Unit tests for process_utils module."""

import io
import sys
import unittest
import subprocess
from unittest.mock import patch

from drift.process_utils import (
    format_output,
    has_admin_privileges,
    check_sudo_privilege,
    run_command,
    run_sudo_command,
)
from drift.constants import set_test_mode


class TestProcessUtils(unittest.TestCase):
    def test_format_output(self) -> None:
        self.assertIsNone(format_output(None))
        self.assertIsNone(format_output(""))
        self.assertIsNone(format_output("   \n\t  "))
        self.assertIsNone(format_output(b""))
        self.assertEqual(format_output("hello world\n"), "hello world")
        self.assertEqual(format_output(b"hello bytes\n"), "hello bytes")

    def test_has_admin_privileges(self) -> None:
        res = has_admin_privileges()
        self.assertIsInstance(res, bool)

    def test_run_command_non_streaming_success(self) -> None:
        res = run_command([sys.executable, "-c", "print('hello stdout')"], text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("hello stdout", res.stdout)

    def test_run_command_non_streaming_debug_logging(self) -> None:
        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.process_utils", level="DEBUG") as cm:
                run_command([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"], text=True)
            logs = "\n".join(cm.output)
            self.assertIn("External:", logs)
            self.assertIn("stdout:\nout", logs)
            self.assertIn("stderr:\nerr", logs)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_run_command_non_streaming_error(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            run_command([sys.executable, "-c", "import sys; print('failing', file=sys.stderr); sys.exit(42)"], text=True)
        self.assertEqual(ctx.exception.returncode, 42)
        self.assertIn("failing", ctx.exception.stderr)

    def test_run_command_streaming_captures_and_streams(self) -> None:
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()

        with patch("sys.stdout", captured_stdout), patch("sys.stderr", captured_stderr):
            res = run_command(
                [sys.executable, "-c", "import sys; print('line 1'); print('line 2'); print('err line', file=sys.stderr)"],
                streaming=True,
                text=True
            )

        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "line 1\nline 2\n")
        self.assertEqual(res.stderr, "err line\n")
        self.assertEqual(captured_stdout.getvalue(), "line 1\nline 2\n")
        self.assertEqual(captured_stderr.getvalue(), "err line\n")

    def test_run_command_streaming_no_debug_dump(self) -> None:
        set_test_mode(True, enable_logging=True)
        captured = io.StringIO()
        try:
            with self.assertLogs("drift.process_utils", level="DEBUG") as cm, patch("sys.stdout", captured):
                run_command(
                    [sys.executable, "-c", "print('live output')"],
                    streaming=True,
                    text=True
                )
            logs = "\n".join(cm.output)
            self.assertIn("External:", logs)
            self.assertNotIn("stdout:\nlive output", logs)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_run_command_streaming_error(self) -> None:
        captured_stderr = io.StringIO()
        with patch("sys.stderr", captured_stderr):
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                run_command(
                    [sys.executable, "-c", "import sys; print('fatal error', file=sys.stderr); sys.exit(5)"],
                    streaming=True,
                    text=True
                )
        self.assertEqual(ctx.exception.returncode, 5)
        self.assertIn("fatal error", ctx.exception.stderr)
        self.assertIn("fatal error", captured_stderr.getvalue())

    def test_run_command_streaming_timeout(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            run_command(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                streaming=True,
                text=True,
                timeout=0.1
            )
        self.assertEqual(ctx.exception.timeout, 0.1)

    def test_run_sudo_command_passthrough(self) -> None:
        with patch("drift.process_utils.has_admin_privileges", return_value=True):
            res = run_sudo_command([sys.executable, "-c", "print('sudo ok')"], sudo=True, text=True)
            self.assertEqual(res.returncode, 0)
            self.assertIn("sudo ok", res.stdout)


if __name__ == "__main__":
    unittest.main()
