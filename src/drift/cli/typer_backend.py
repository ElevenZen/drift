import sys
from typing import Optional, List
from pathlib import Path

import typer
from rich import print as rprint

from .actions import (
    get_drift_root,
    execute_render,
    execute_init,
    execute_stage,
    execute_render_commit,
    execute_apply,
    execute_install_commit,
    execute_reverse_sync,
    execute_new,
    execute_uninstall,
    execute_status,
    execute_gc,
    execute_diff,
    execute_add,
    execute_adopt,
    execute_rollback
)

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
    ),
    verbose: bool = typer.Option(
        False,
        "-v",
        "--verbose",
        help="Enable verbose (DEBUG) logging output"
    )
) -> None:
    if verbose:
        from . import setup_logging
        import logging
        setup_logging(level=logging.DEBUG)
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
        rprint("[bold yellow]📝[/bold yellow] [bold green]Generated config/envsubst.bash and config/mustache.envst.json.[/bold green]")
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

@app.command("reverse-sync")
def typer_reverse_sync(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to reverse-sync specifically"
    )
) -> None:
    """Synchronize changes from host system back to install/ state database."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_reverse_sync(drift_root, packages)
        if packages:
            pkgs_str = ", ".join(packages)
            rprint(f"[bold yellow]✨[/bold yellow] [bold green]Successfully reverse-synced package(s) '{pkgs_str}'![/bold green]")
        else:
            rprint("[bold yellow]✨[/bold yellow] [bold green]Successfully reverse-synced all enabled packages![/bold green]")
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)

@app.command("new")
def typer_new(
    ctx: typer.Context,
    package_name: str = typer.Argument(
        ...,
        help="Name of the new package to create"
    ),
    config_filename: Optional[str] = typer.Argument(
        None,
        help="Explicitly name the config file (defaults to package.toml)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Forcefully overwrite any existing config file inside the package"
    ),
    target: Optional[str] = typer.Option(
        None,
        "--target",
        "-t",
        help="Explicitly configure the deployment target directory inside package.toml"
    ),
    method: Optional[str] = typer.Option(
        None,
        "--method",
        "-m",
        help="Explicitly configure the installation method ('stow' or 'copy') inside package.toml"
    )
) -> None:
    """Scaffold a new package directory and package.toml."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_new(
            drift_root,
            package_name,
            config_filename=config_filename,
            force=force,
            target_directory=target,
            install_method=method
        )
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("uninstall")
def typer_uninstall(
    ctx: typer.Context,
    packages: List[str] = typer.Argument(
        ...,
        help="One or more package names to uninstall"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force uninstallation even if package is still active in drift.toml"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview uninstallation without making changes"
    ),
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Remove management relationship but keep configurations as actual physical files on host system"
    )
) -> None:
    """Uninstall a package from the system and restore any backups."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_uninstall(drift_root, packages, force=force, dry_run=dry_run, detach=detach)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("adopt")
def typer_adopt(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to adopt specifically"
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Interactively reconcile each drifted file"
    ),
    accept_conflicts: bool = typer.Option(
        False,
        "--accept-conflicts",
        help="Apply conflicting patches, writing merge conflict markers directly into templates"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force adoption even if the package source directory has uncommitted modifications"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Simulate the adoption, previewing changes and conflict results"
    )
) -> None:
    """Adopt active system drifts and incorporate them back into source templates."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_adopt(
            drift_root=drift_root,
            package_names=packages or [],
            interactive=interactive,
            accept_conflicts=accept_conflicts,
            force=force,
            dry_run=dry_run
        )
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("status")
def typer_status(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to audit specifically"
    )
) -> None:
    """Audit and aggregate configuration status across active packages."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_status(drift_root, packages)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("gc")
def typer_gc(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Simulate the garbage collection without making changes"
    )
) -> None:
    """Identify and uninstall orphan packages (present in state but disabled in config)."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_gc(drift_root, dry_run=dry_run)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("diff")
def typer_diff(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional package name(s) to diff specifically"
    ),
    template: bool = typer.Option(
        False,
        "--template", "-t",
        help="Visualize Template Evolution (Diff A: src -> render)"
    ),
    system: bool = typer.Option(
        False,
        "--system", "-s",
        help="Visualize System Drift (Diff B: System -> install)"
    ),
    side_by_side: bool = typer.Option(
        False,
        "--side-by-side", "-y",
        help="Show side-by-side comparison (Note: relies on git/diff capabilities)"
    ),
    stat: bool = typer.Option(
        False,
        "--stat",
        help="Show concise summary of changes (diffstat)"
    )
) -> None:
    """Visualize changes between configuration layers (Default: Pending Delta / Diff Δ)."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        
        diff_type = "pending"
        if template:
            diff_type = "template"
        elif system:
            diff_type = "system"
            
        execute_diff(drift_root, packages, diff_type=diff_type, side_by_side=side_by_side, stat=stat)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("add")
def typer_add(
    ctx: typer.Context,
    package_name: str = typer.Argument(
        ...,
        help="Name of the package to add resources into"
    ),
    paths: List[str] = typer.Argument(
        ...,
        help="One or more file/folder paths to import"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the import without making changes"
    )
) -> None:
    """Import files or folders from the system into a package (with dot-prefix translation)."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_add(drift_root, package_name, paths, dry_run=dry_run)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)


@app.command("rollback")
def typer_rollback(
    ctx: typer.Context,
    packages: Optional[List[str]] = typer.Argument(
        None,
        help="Optional specific package(s) to rollback"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force the rollback and skip failed interlock checking"
    )
) -> None:
    """Rollback failed deployments and restore systems to the last committed clean state."""
    try:
        cli_ctx: DriftCLIContext = ctx.obj
        drift_root = cli_ctx.get_drift_root()
        execute_rollback(drift_root, packages, force=force)
    except Exception as e:
        rprint(f"[bold red]❌ [ERROR][/bold red] [red]{e}[/red]", file=sys.stderr)
        raise typer.Exit(code=1)
