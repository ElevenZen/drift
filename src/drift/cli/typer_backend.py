import sys
from typing import Optional, List
from pathlib import Path

import typer
from rich import print as rprint

from .actions import get_drift_root, execute_render, execute_init, execute_stage, execute_render_commit, execute_apply, execute_install_commit

app = typer.Typer(
    help="drift: Decoupled Two-Stage Git-Backed Dotfiles Manager",
    add_completion=False,
    no_args_is_help=True
)

class DriftCLIContext:
    """CLI context helper to hold parameters and resolve directory root with type safety."""
    def __init__(self, directory: Optional[str] = None, no_git_root: bool = False) -> None:
        self.directory: Optional[str] = directory
        self.no_git_root: bool = no_git_root

    def get_drift_root(self) -> Path:
        """Resolves the absolute path to the drift root repository."""
        base_dir = Path(self.directory).resolve() if self.directory else Path.cwd().resolve()
        if self.no_git_root:
            return base_dir
        return get_drift_root(base_dir)


@app.callback()
def main_callback(
    ctx: typer.Context,
    directory: Optional[str] = typer.Option(
        None,
        "-C",
        "--directory",
        help="Run as if drift was started in <directory> instead of current working directory"
    ),
    no_git_root: bool = typer.Option(
        False,
        "--no-git-root",
        help="Stop resolving git root of cwd or -C directory, using the literal path instead"
    )
) -> None:
    ctx.obj = DriftCLIContext(directory=directory, no_git_root=no_git_root)


@app.command("init")
def typer_init(
    ctx: typer.Context,
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force re-initialization and overwrite existing files"
    )
) -> None:
    """Initialize a new drift workspace."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        # Bypassing show-toplevel check for init, using raw directory/cwd as root
        drift_root = Path(cli_ctx.directory).resolve() if cli_ctx.directory else Path.cwd().resolve()
        execute_init(drift_root, force=force, no_git_root=cli_ctx.no_git_root)
        rprint("[bold yellow]✨[/bold yellow] [bold green]Initialized drift workspace![/bold green]")
        rprint("[bold yellow]📁[/bold yellow] [bold green]Created render/ sandbox Git database.[/bold green]")
        rprint("[bold yellow]📁[/bold yellow] [bold green]Created install/ local state Git database.[/bold green]")
        rprint("[bold yellow]📝[/bold yellow] [bold green]Generated drift.toml template.[/bold green]")
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("render")
def typer_render(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to render specifically"
    )
) -> None:
    """Render templates of a package or all enabled packages."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_render(drift_root, packages)
        if packages:
            pkgs_str = ", ".join(packages)
            rprint(f"[bold yellow]✨[/bold yellow] [bold green]Successfully rendered package(s) '{pkgs_str}'![/bold green]")
        else:
            rprint("[bold yellow]✨[/bold yellow] [bold green]Successfully rendered all enabled packages![/bold green]")
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("stage")
def typer_stage(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to stage specifically"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force staging and bypass uncommitted modifications check"
    )
) -> None:
    """Stage compiled sandbox templates from render/ to install/ state database."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_stage(drift_root, packages, force=force)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("apply")
def typer_apply(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to apply specifically"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force deployment and bypass check"
    )
) -> None:
    """Apply configurations from state database to active host system."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_apply(drift_root, packages, force=force)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("render-commit")
def typer_render_commit(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to commit specifically"
    ),
    message: str = typer.Option(
        ...,
        "-m",
        "--message",
        help="Commit message"
    )
) -> None:
    """Stage and commit compiled render sandbox changes."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_render_commit(drift_root, message, packages)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("install-commit")
def typer_install_commit(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to commit specifically"
    ),
    message: str = typer.Option(
        ...,
        "-m",
        "--message",
        help="Commit message"
    )
) -> None:
    """Stage and commit install state directory changes."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_install_commit(drift_root, message, packages)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)
