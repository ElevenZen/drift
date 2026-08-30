"""Unit tests for Drift shell tab-completion generators (Bash, Zsh, Fish)."""

import unittest
from drift.cli.schema import build_completion_schema, SHELLS
from drift.cli.completion import (
    BashGenerator,
    ZshGenerator,
    FishGenerator,
    generate_completion_script,
)


class TestCompletionGenerators(unittest.TestCase):
    """Test suite verifying compilation of shell tab-completion scripts."""

    def setUp(self):
        self.schema = build_completion_schema()

    def test_bash_generator_output(self):
        """Verifies Bash script generation and structural components."""
        script = generate_completion_script("bash", self.schema)
        self.assertIsInstance(script, str)
        self.assertIn("# Bash completion script for drift", script)
        self.assertIn("_drift_packages()", script)
        self.assertIn("_drift_completion()", script)
        self.assertIn("complete -F _drift_completion drift", script)

        # Check that all subcommands are present
        for cmd_name in self.schema.commands.keys():
            self.assertIn(f'"{cmd_name}"', script)

        # Check options
        self.assertIn("--no-git-root", script)
        self.assertIn("--json", script)
        self.assertIn("--verbose", script)

    def test_zsh_generator_output(self):
        """Verifies Zsh script generation with interactive descriptions."""
        script = generate_completion_script("zsh", self.schema)
        self.assertIsInstance(script, str)
        self.assertIn("#compdef drift", script)
        self.assertIn("_drift_packages()", script)
        self.assertIn("_drift()", script)
        self.assertIn("_arguments", script)
        self.assertIn("_describe", script)

        # Check that subcommands with descriptions are present
        for cmd_name, cmd in self.schema.commands.items():
            self.assertIn(f"'{cmd_name}:", script)

        # Check that choice helper functions were generated
        self.assertIn("_drift_hook_name_choices()", script)
        self.assertIn("_drift_topic_choices()", script)
        self.assertIn("pre_source", script)
        self.assertIn("post_render", script)

    def test_fish_generator_output(self):
        """Verifies Fish script generation with declarative complete rules."""
        script = generate_completion_script("fish", self.schema)
        self.assertIsInstance(script, str)
        self.assertIn("# Fish completion script for drift", script)
        self.assertIn("complete -c drift -f", script)
        self.assertIn("function __drift_packages", script)
        self.assertIn("__fish_use_subcommand", script)

        # Check that subcommands are present
        for cmd_name in self.schema.commands.keys():
            self.assertIn(f'-a "{cmd_name}"', script)

        # Check that fixed choices with descriptions are present
        self.assertIn("pre_source", script)
        self.assertIn("post_render", script)
        self.assertIn("stow", script)
        self.assertIn("copy", script)

    def test_supported_shells(self):
        """Verifies that all registered shells generate non-empty scripts."""
        for shell_choice in SHELLS:
            script = generate_completion_script(shell_choice.value)
            self.assertTrue(len(script) > 100)

    def test_invalid_shell_raises_error(self):
        """Verifies that requesting an unsupported shell raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            generate_completion_script("powershell")
        self.assertIn("Unsupported shell 'powershell'", str(ctx.exception))

    def test_cli_complete_command_argparse(self):
        """Verifies 'drift complete bash' and 'drift complete --json' via Argparse."""
        import json
        from unittest.mock import patch
        from drift.cli.argparse_backend import run_argparse_cli

        with patch("sys.stdout.write") as mock_stdout:
            run_argparse_cli(["complete", "bash"])
            output = "".join(call.args[0] for call in mock_stdout.call_args_list)
            self.assertIn("# Bash completion script for drift", output)

        with patch("sys.stdout.write") as mock_stdout:
            run_argparse_cli(["complete", "zsh", "--json"])
            output = "".join(call.args[0] for call in mock_stdout.call_args_list)
            data = json.loads(output)
            self.assertEqual(data["command"], "complete")
            self.assertEqual(data["status"], "SUCCESS")
            self.assertEqual(data["shell"], "zsh")
            self.assertIn("#compdef drift", data["script"])

    def test_cli_complete_command_typer(self):
        """Verifies 'drift complete fish' via Typer."""
        from typer.testing import CliRunner
        from drift.cli.typer_backend import app

        runner = CliRunner()
        res = runner.invoke(app, ["complete", "fish"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("complete -c drift -f", res.output)


if __name__ == "__main__":
    unittest.main()
