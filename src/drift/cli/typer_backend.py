import os
import sys
from typing import Optional

import typer
from rich import print as rprint

from .actions import execute_render

app = typer.Typer(
    help="drift: Decoupled Two-Stage Git-Backed Dotfiles Manager",
    add_completion=False,
    no_args_is_help=True
)


@app.callback()
def main_callback(
    ctx: typer.Context,
    directory: Optional[str] = typer.Option(
        None,
        "-C",
        "--directory",
        help="Run as if drift was started in <directory> instead of current working directory"
    )
) -> None:
    ctx.obj = {"directory": directory}


@app.command("render")
def typer_render(
    ctx: typer.Context,
    package: Optional[str] = typer.Argument(
        None,
        help="Optional package name to render specifically"
    )
) -> None:
    """Render templates of a package or all enabled packages."""
    directory = ctx.obj.get("directory") if ctx.obj else None

    if directory:
        drift_root = os.path.abspath(directory)
    else:
        drift_root = os.getcwd()

    try:
        execute_render(drift_root, package)
        if package:
            rprint(f"[bold yellow]✨[/bold yellow] [bold green]Successfully rendered package '{package}'![/bold green]")
        else:
            rprint("[bold yellow]✨[/bold yellow] [bold green]Successfully rendered all enabled packages![/bold green]")
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)
