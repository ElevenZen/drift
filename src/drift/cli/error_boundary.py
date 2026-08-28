"""Centralized CLI error boundary context manager for Drift commands."""

import sys
from contextlib import contextmanager
from ..exceptions import DriftError
from ..constants import ExitCode


@contextmanager
def cli_error_boundary(json_mode: bool = False, use_rich: bool = False):
    """Intercepts exceptions, formats user-facing error messages, and terminates with standard exit codes."""
    try:
        yield
    except SystemExit as se:
        code = se.code if se.code is not None else ExitCode.SUCCESS
        if use_rich:
            import typer
            raise typer.Exit(code=code)
        sys.exit(code)
    except DriftError as de:
        if not json_mode:
            if use_rich:
                from rich import print as rprint
                rprint(f"[bold red]❌ [ERROR][/bold red] [red]{de}[/red]", file=sys.stderr)
            else:
                print(f"❌ [ERROR] {de}", file=sys.stderr)
        if use_rich:
            import typer
            raise typer.Exit(code=de.exit_code)
        sys.exit(de.exit_code)
    except Exception as e:
        if not json_mode:
            if use_rich:
                from rich import print as rprint
                rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
            else:
                print(f"❌ [ERROR] {e}", file=sys.stderr)
        if use_rich:
            import typer
            raise typer.Exit(code=ExitCode.GENERAL_ERROR)
        sys.exit(ExitCode.GENERAL_ERROR)
