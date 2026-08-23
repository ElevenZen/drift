import unittest
from io import StringIO
import sys
from unittest.mock import patch

from drift.cli.help_docs import get_help_page, print_help_document
from drift.constants import CONFIG_DIR_NAME, SECRETS_ENV_FILE_NAME


class TestHelpDocs(unittest.TestCase):
    def test_get_help_page_valid_topics(self) -> None:
        """Verifies that all valid help topics return appropriate documentation content."""
        # Default/None overall page
        overall = get_help_page(None)
        self.assertIn("Next-Gen Transactional Dotfile Manager", overall)
        self.assertIn("The Drift Data-Flow Loop", overall)

        # package
        pkg = get_help_page("package")
        self.assertIn("The 'Package' Concept in Drift", pkg)

        # src
        src = get_help_page("src")
        self.assertIn("Declarative Source Directory: `src/`", src)

        # render
        render = get_help_page("render")
        self.assertIn("Sandbox Compilation & Render Directory: `render/`", render)

        # install
        install = get_help_page("install")
        self.assertIn("Local State Database & Installation: `install/`", install)

        # drift_package.toml
        pkg_toml = get_help_page("drift_package.toml")
        self.assertIn("drift_package.toml Complete Configuration Reference", pkg_toml)

        # drift_package.toml fallback
        drift_pkg_toml = get_help_page("drift_package.toml")
        self.assertIn("drift_package.toml Complete Configuration Reference", drift_pkg_toml)

        # drift.toml
        drift_toml = get_help_page("drift.toml")
        self.assertIn("drift.toml Complete Global Configuration Reference", drift_toml)

        # ignore
        ignore_doc = get_help_page("ignore")
        self.assertIn("Drift Ignore Engine: Syntax and Integration", ignore_doc)

        # fcd
        fcd_doc = get_help_page("fcd")
        self.assertIn("Fully-Controlled Directories (FCDs): Tracking Active File Creation", fcd_doc)

        # workspace
        workspace_doc = get_help_page("workspace")
        self.assertIn("drift Workspace & Configuration Overrides", workspace_doc)
        self.assertIn("Dual-Layered Configuration Merging", workspace_doc)
        self.assertIn(f"Environment Secret Vault (`{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}`)", workspace_doc)

    def test_get_help_page_invalid_topic_raises_error(self) -> None:
        """Verifies that querying an unknown/invalid help topic raises ValueError."""
        with self.assertRaises(ValueError) as context:
            get_help_page("invalid_topic")
        self.assertIn("Unknown help topic: 'invalid_topic'", str(context.exception))
        self.assertIn("'fcd'", str(context.exception))
        self.assertIn("'ignore'", str(context.exception))
        self.assertIn("'workspace'", str(context.exception))

    @patch("sys.stdout.isatty")
    def test_print_help_document_falls_back_to_print_when_not_tty(self, mock_isatty) -> None:
        """Verifies that when output is not a TTY (tty=False), print_help_document writes directly to stdout."""
        mock_isatty.return_value = False

        stdout_capture = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout_capture

        try:
            print_help_document("package")
        finally:
            sys.stdout = original_stdout

        output = stdout_capture.getvalue()
        self.assertIn("The 'Package' Concept in Drift", output)


if __name__ == "__main__":
    unittest.main()
