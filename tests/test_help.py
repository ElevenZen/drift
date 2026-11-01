import unittest
from io import StringIO
import sys
from unittest.mock import patch

from drift.cli import main
from drift.cli.argparse_backend import run_argparse_cli
from drift.cli.help_docs import get_help_page, print_help_document
from drift.core.constants import CONFIG_DIR_NAME, SECRETS_ENV_FILE_NAME


class TestHelpDocs(unittest.TestCase):
    def test_get_help_page_valid_topics(self) -> None:
        """Verifies that all valid help topics return appropriate documentation content."""
        # Default/None overall page
        overall = get_help_page(None)
        self.assertIn("Next-Gen Transactional Dotfile Manager", overall)
        self.assertIn("The Drift Data-Flow Loop", overall)

        # Explicit overall / overview topic
        self.assertEqual(get_help_page("overall"), overall)
        self.assertEqual(get_help_page("overview"), overall)

        # package
        pkg = get_help_page("package")
        self.assertIn("The 'Package' Concept in Drift", pkg)
        self.assertIn("Lifecycle Hooks Matrix", pkg)

        # src
        src = get_help_page("src")
        self.assertIn("Declarative Source Directory: `src/`", src)

        # render
        render = get_help_page("render")
        self.assertIn("Sandbox Compilation & Render Directory: `render/`", render)

        # install
        install = get_help_page("install")
        self.assertIn("Local State Database & Installation: `install/`", install)

        # deploy
        deploy = get_help_page("deploy")
        self.assertIn("Composite Deployment Pipeline (`drift deploy`)", deploy)
        self.assertIn("The 5-Step Composite Execution Pipeline", deploy)

        # drift_package.toml
        pkg_toml = get_help_page("drift_package.toml")
        self.assertIn("drift_package.toml Complete Configuration Reference", pkg_toml)
        self.assertIn("Lifecycle Hooks Execution Matrix", pkg_toml)

        # drift_package.toml fallback
        drift_pkg_toml = get_help_page("drift_package.toml")
        self.assertIn("drift_package.toml Complete Configuration Reference", drift_pkg_toml)

        # drift_workspace.toml
        drift_ws_toml = get_help_page("drift_workspace.toml")
        self.assertIn("drift_workspace.toml Complete Global Configuration Reference", drift_ws_toml)

        # ignore
        ignore_doc = get_help_page("ignore")
        self.assertIn("Drift Ignore Engine: Syntax and Integration", ignore_doc)

        # fcd
        fcd_doc = get_help_page("fcd")
        self.assertIn("Fully-Controlled Directories (FCDs): Tracking Active File Creation", fcd_doc)

        # workspace
        workspace_doc = get_help_page("workspace")
        self.assertIn("drift Workspace & Configuration Overrides", workspace_doc)
        self.assertIn("Configuration Merging & Python Hooks", workspace_doc)
        self.assertIn("Dynamic Python Workspace Hook", workspace_doc)
        self.assertIn(f"Environment Secret Vault (`{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}`)", workspace_doc)

        # health
        health_doc = get_help_page("health")
        self.assertIn("Drift Package Runtime Health Checks", health_doc)

        # rollback
        rollback_doc = get_help_page("rollback")
        self.assertIn("Transactional Rollback & Auto-Rollback Configuration (`drift rollback`)", rollback_doc)
        self.assertIn("auto_rollback", rollback_doc)
        self.assertIn("Write-Ahead Log", rollback_doc)

        # clone
        clone_doc = get_help_page("clone")
        self.assertIn("Drift Repository Cloning & Bootstrapping", clone_doc)

        # faq
        faq_doc = get_help_page("faq")
        self.assertIn("Drift Frequently Asked Questions & Troubleshooting (FAQ)", faq_doc)
        self.assertIn("drift deploy --force", faq_doc)
        self.assertIn("--no-hooks", faq_doc)

    def test_get_help_page_aliases(self) -> None:
        """Verifies that topic aliases map correctly to their canonical documentation pages."""
        pkg_toml = get_help_page("drift_package.toml")
        ws_toml = get_help_page("drift_workspace.toml")
        overall = get_help_page("overall")
        install = get_help_page("install")
        deploy = get_help_page("deploy")
        workspace = get_help_page("workspace")
        health = get_help_page("health")
        rollback = get_help_page("rollback")
        clone = get_help_page("clone")
        faq = get_help_page("faq")
        fcd = get_help_page("fcd")
        ignore = get_help_page("ignore")

        # package config aliases
        for alias in ("package_config", "pkg_config", "package-config", "pkg-config", "drift_package", "drift_package_toml", "package.toml", "pkg.toml"):
            self.assertEqual(get_help_page(alias), pkg_toml, f"Alias '{alias}' failed to resolve to drift_package.toml")

        # workspace config aliases
        for alias in ("workspace_config", "ws_config", "workspace-config", "ws-config", "drift_workspace", "drift_workspace_toml", "workspace.toml", "ws.toml", "drift.toml", "drift_toml"):
            self.assertEqual(get_help_page(alias), ws_toml, f"Alias '{alias}' failed to resolve to drift_workspace.toml")

        # other topic aliases
        self.assertEqual(get_help_page("arch"), overall)
        self.assertEqual(get_help_page("intro"), overall)
        self.assertEqual(get_help_page("apply"), install)
        self.assertEqual(get_help_page("staging"), install)
        self.assertEqual(get_help_page("deployment"), deploy)
        self.assertEqual(get_help_page("ws"), workspace)
        self.assertEqual(get_help_page("secrets"), workspace)
        self.assertEqual(get_help_page("check"), health)
        self.assertEqual(get_help_page("hooks"), health)
        for alias in ("auto_rollback", "auto-rollback", "wal", "recovery"):
            self.assertEqual(get_help_page(alias), rollback, f"Alias '{alias}' failed to resolve to rollback")
        self.assertEqual(get_help_page("migrate"), clone)
        self.assertEqual(get_help_page("troubleshooting"), faq)
        self.assertEqual(get_help_page("controlled_dirs"), fcd)
        self.assertEqual(get_help_page(".drift_ignore"), ignore)

    def test_get_help_page_invalid_topic_raises_error(self) -> None:
        """Verifies that querying an unknown/invalid help topic raises ValueError."""
        with self.assertRaises(ValueError) as context:
            get_help_page("invalid_topic")
        self.assertIn("Unknown help topic: 'invalid_topic'", str(context.exception))
        self.assertIn("'fcd'", str(context.exception))
        self.assertIn("'ignore'", str(context.exception))
        self.assertIn("'workspace'", str(context.exception))
        self.assertIn("'deploy'", str(context.exception))
        self.assertIn("'rollback'", str(context.exception))

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

    @patch("sys.stdout.isatty", return_value=False)
    def test_cli_help_rollback_dual_backends(self, _mock_isatty) -> None:
        """Verifies `drift help rollback` across both Typer (`main`) and Argparse (`run_argparse_cli`) backends."""
        with patch("sys.stdout", StringIO()) as out_typer:
            main(["help", "rollback"])
            self.assertIn("Transactional Rollback & Auto-Rollback Configuration", out_typer.getvalue())

        with patch("sys.stdout", StringIO()) as out_argparse:
            run_argparse_cli(["help", "rollback"])
            self.assertIn("Transactional Rollback & Auto-Rollback Configuration", out_argparse.getvalue())


if __name__ == "__main__":
    unittest.main()
