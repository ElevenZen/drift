"""Unit tests for process_utils module."""

import io
import sys
import unittest
import subprocess
from unittest.mock import patch

from drift.process_utils import (
    strip_ansi,
    format_output,
    has_admin_privileges,
    check_sudo_privilege,
    run_command,
    run_sudo_command,
)
from drift.constants import set_test_mode


class TestProcessUtils(unittest.TestCase):
    def test_strip_ansi(self) -> None:
        self.assertEqual(strip_ansi(None), "")
        self.assertEqual(strip_ansi(""), "")
        self.assertEqual(strip_ansi("plain text"), "plain text")
        # Color codes
        self.assertEqual(strip_ansi("\x1b[31mRed Text\x1b[0m"), "Red Text")
        self.assertEqual(strip_ansi("\x1b[1;32;40mBold Green\x1b[0m"), "Bold Green")
        self.assertEqual(strip_ansi("\x1b[38;5;196m256 Color\x1b[0m"), "256 Color")
        self.assertEqual(strip_ansi("\x1b[38;2;255;128;0mRGB Color\x1b[0m"), "RGB Color")
        # Style & Cursor sequences
        self.assertEqual(strip_ansi("\x1b[1m\x1b[4mUnderline Bold\x1b[22m\x1b[24m"), "Underline Bold")
        self.assertEqual(strip_ansi("\x1b[2K\x1b[1GProgress Line"), "Progress Line")
        # OSC escape sequences
        self.assertEqual(strip_ansi("\x1b]0;Window Title\x07Message"), "Message")
        # Bytes decoding
        self.assertEqual(strip_ansi(b"\x1b[33mYellow Bytes\x1b[0m"), "Yellow Bytes")

    def test_format_output(self) -> None:
        self.assertIsNone(format_output(None))
        self.assertIsNone(format_output(""))
        self.assertIsNone(format_output("   \n\t  "))
        self.assertIsNone(format_output(b""))
        self.assertEqual(format_output("hello world\n"), "hello world")
        self.assertEqual(format_output(b"hello bytes\n"), "hello bytes")
        self.assertEqual(format_output("\x1b[31;1mError: file missing\x1b[0m\n"), "Error: file missing")

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
            self.assertIn("Command finished with exit code 0:", logs)
            self.assertIn("stdout:\nout", logs)
            self.assertIn("stderr:\nerr", logs)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_run_command_non_streaming_error(self) -> None:
        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.process_utils", level="ERROR") as cm:
                with self.assertRaises(subprocess.CalledProcessError) as ctx:
                    run_command([sys.executable, "-c", "import sys; print('failing', file=sys.stderr); sys.exit(42)"], text=True)
            self.assertEqual(ctx.exception.returncode, 42)
            self.assertIn("failing", ctx.exception.stderr)
            logs = "\n".join(cm.output)
            self.assertIn("Command failed with exit code 42:", logs)
        finally:
            set_test_mode(True, enable_logging=False)

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

    def test_run_command_strips_ansi_in_debug_logs_and_errors(self) -> None:
        set_test_mode(True, enable_logging=True)
        try:
            # 1. Non-streaming success cleans debug logs and CompletedProcess
            with self.assertLogs("drift.process_utils", level="DEBUG") as cm:
                res = run_command([
                    sys.executable, "-c",
                    "import sys; print('\\x1b[32mSUCCESS_COLOR\\x1b[0m'); print('\\x1b[33mWARN_COLOR\\x1b[0m', file=sys.stderr)"
                ], text=True)
            logs = "\n".join(cm.output)
            self.assertIn("stdout:\nSUCCESS_COLOR", logs)
            self.assertIn("stderr:\nWARN_COLOR", logs)
            self.assertNotIn("\x1b[32m", logs)
            self.assertNotIn("\x1b[33m", logs)
            self.assertEqual(res.stdout.strip(), "SUCCESS_COLOR")
            self.assertEqual(res.stderr.strip(), "WARN_COLOR")

            # 2. Non-streaming error cleans debug logs and CalledProcessError
            with self.assertLogs("drift.process_utils", level="DEBUG") as cm:
                with self.assertRaises(subprocess.CalledProcessError) as ctx:
                    run_command([
                        sys.executable, "-c",
                        "import sys; print('\\x1b[31;1mFATAL_ERROR\\x1b[0m', file=sys.stderr); sys.exit(1)"
                    ], text=True)
            logs = "\n".join(cm.output)
            self.assertIn("stderr:\nFATAL_ERROR", logs)
            self.assertNotIn("\x1b[31;1m", logs)
            self.assertEqual(ctx.exception.stderr.strip(), "FATAL_ERROR")
            self.assertNotIn("\x1b[31;1m", ctx.exception.stderr)

            # 3. Streaming error cleans CalledProcessError
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(subprocess.CalledProcessError) as ctx:
                    run_command([
                        sys.executable, "-c",
                        "import sys; print('\\x1b[31mSTREAM_ERR\\x1b[0m', file=sys.stderr); sys.exit(2)"
                    ], streaming=True, text=True)
            self.assertEqual(ctx.exception.stderr.strip(), "STREAM_ERR")
            self.assertNotIn("\x1b[31m", ctx.exception.stderr)
        finally:
            set_test_mode(True, enable_logging=False)


if __name__ == "__main__":
    unittest.main()
