"""Unit tests for Drift shell tab-completion generators (Bash, Zsh, Fish)."""

import unittest
from drift.cli.schema import build_completion_schema, SHELLS
from drift.cli.completion import (
    BashGenerator,
    ZshGenerator,
    FishGenerator,
    NushellGenerator,
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

    def test_nushell_generator_output(self):
        """Verifies Nushell script generation with typed extern declarations and custom completers."""
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        script = generate_completion_script("nu", self.schema)
        self.assertIsInstance(script, str)
        self.assertIn("# Nushell completion script for drift", script)
        self.assertIn('def "nu-complete drift-packages" []', script)
        self.assertIn('def "nu-complete drift-shells" []', script)
        self.assertIn('def "nu-complete drift-help-topics" []', script)
        self.assertIn('export extern "main"', script)
        self.assertIn('export extern "drift deploy"', script)
        self.assertIn('export extern "drift status"', script)
        self.assertIn('export extern "drift complete"', script)
        self.assertIn('--force(-f)', script)
        self.assertIn('string@"nu-complete drift-packages"', script)
        self.assertIn('string@"nu-complete drift-shells"', script)

        # If nu executable is present in PATH, verify it compiles cleanly without errors
        if shutil.which("nu"):
            with tempfile.TemporaryDirectory() as td:
                drift_nu = Path(td) / "drift.nu"
                drift_nu.write_text(script)
                res = subprocess.run(["nu", "-c", f"use {drift_nu} *; help drift; help drift deploy"], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"Nushell parse error: {res.stderr}")

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

    def test_cli_complete_install_argparse(self):
        """Verifies 'drift complete zsh --install' writes file and reports path."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from drift.cli.argparse_backend import run_argparse_cli

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with patch("pathlib.Path.home", return_value=fake_home):
                with patch("sys.stdout.write") as mock_stdout:
                    run_argparse_cli(["complete", "zsh", "--install"])
                    output = "".join(call.args[0] for call in mock_stdout.call_args_list)
                    self.assertIn("Installed zsh completion script to:", output)
                    target_file = fake_home / ".local" / "share" / "zsh" / "site-functions" / "_drift"
                    self.assertTrue(target_file.is_file())
                    content = target_file.read_text()
                    self.assertIn("#compdef drift", content)

    def test_cli_complete_install_json(self):
        """Verifies 'drift complete fish --install --json' outputs JSON with installed list."""
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from drift.cli.argparse_backend import run_argparse_cli

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with patch("pathlib.Path.home", return_value=fake_home):
                with patch("sys.stdout.write") as mock_stdout:
                    run_argparse_cli(["complete", "fish", "--install", "--json"])
                    output = "".join(call.args[0] for call in mock_stdout.call_args_list)
                    data = json.loads(output)
                    self.assertEqual(data["status"], "SUCCESS")
                    self.assertEqual(len(data["installed"]), 1)
                    self.assertEqual(data["installed"][0]["shell"], "fish")
                    fish_path = Path(data["installed"][0]["path"])
                    self.assertTrue(fish_path.is_file())

    def test_cli_complete_install_all_shells(self):
        """Verifies 'drift complete --install' writes completion files for all supported shells."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from drift.cli.argparse_backend import run_argparse_cli

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with patch("pathlib.Path.home", return_value=fake_home):
                with patch("sys.stdout.write") as mock_stdout:
                    run_argparse_cli(["complete", "--install"])
                    output = "".join(call.args[0] for call in mock_stdout.call_args_list)
                    self.assertIn("Installed bash completion script to:", output)
                    self.assertIn("Installed zsh completion script to:", output)
                    self.assertIn("Installed fish completion script to:", output)
                    self.assertIn("Installed nu completion script to:", output)

                    bash_file = fake_home / ".local" / "share" / "bash-completion" / "completions" / "drift"
                    zsh_file = fake_home / ".local" / "share" / "zsh" / "site-functions" / "_drift"
                    fish_file = fake_home / ".config" / "fish" / "completions" / "drift.fish"
                    nu_file = fake_home / ".config" / "nushell" / "completions" / "drift.nu"

                    self.assertTrue(bash_file.is_file())
                    self.assertTrue(zsh_file.is_file())
                    self.assertTrue(fish_file.is_file())
                    self.assertTrue(nu_file.is_file())

    def test_cli_complete_install_nushell(self):
        """Verifies 'drift complete nu --install' writes file to standard nushell path."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from drift.cli.argparse_backend import run_argparse_cli

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            with patch("pathlib.Path.home", return_value=fake_home):
                with patch("sys.stdout.write") as mock_stdout:
                    run_argparse_cli(["complete", "nu", "--install"])
                    output = "".join(call.args[0] for call in mock_stdout.call_args_list)
                    self.assertIn("Installed nu completion script to:", output)
                    target_file = fake_home / ".config" / "nushell" / "completions" / "drift.nu"
                    self.assertTrue(target_file.is_file())
                    content = target_file.read_text()
                    self.assertIn('export extern "main"', content)
                    self.assertIn('export extern "drift deploy"', content)


if __name__ == "__main__":
    unittest.main()
