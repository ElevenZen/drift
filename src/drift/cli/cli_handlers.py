import re
from pathlib import Path
from typing import Optional, List, Any, Dict, Callable

from .actions import (
    get_drift_root,
    execute_render,
    execute_init,
    execute_stage,
    execute_render_commit,
    execute_apply,
    execute_install_commit,
    execute_reverse_sync,
    execute_new_package,
    execute_uninstall,
    execute_status,
    execute_gc,
    execute_diff,
    execute_add,
    execute_adopt,
    execute_rollback,
    execute_deploy,
    execute_repair,
    execute_health,
    execute_clone,
    execute_help,
    execute_hook,
    execute_complete,
)
from ..result_models import DiffType
from .error_boundary import cli_error_boundary


class DriftCLIContext:
    """Unified CLI context helper holding configuration parameters and resolving directory root."""
    def __init__(
        self,
        directory: Optional[str] = None,
        no_git_root: bool = False,
        json_mode: bool = False,
        use_rich: bool = True
    ) -> None:
        self.directory: Optional[str] = directory
        self.no_git_root: bool = no_git_root
        self.json_mode: bool = json_mode
        self.use_rich: bool = use_rich

    def get_drift_root(self) -> Path:
        """Resolves the absolute path to the drift root repository."""
        base_dir = Path(self.directory).resolve() if self.directory else Path.cwd().resolve()
        if self.no_git_root:
            return base_dir
        return get_drift_root(base_dir)

    def print_message(self, rich_text: str, plain_text: Optional[str] = None) -> None:
        """Prints user-facing feedback honoring json_mode and rich formatting capabilities."""
        if self.json_mode:
            return
        if self.use_rich:
            try:
                from rich import print as rprint
                rprint(rich_text)
                return
            except ImportError:
                pass
        text = plain_text if plain_text is not None else re.sub(r"\[/?[\w\s=,#]+\]", "", rich_text)
        print(text)


def _extract_cli_context(ctx: Any) -> DriftCLIContext:
    """Safely extracts a DriftCLIContext instance from either DriftCLIContext or a Typer context."""
    if isinstance(ctx, DriftCLIContext):
        return ctx
    if hasattr(ctx, "obj") and isinstance(ctx.obj, DriftCLIContext):
        return ctx.obj
    return DriftCLIContext()


# =============================================================================
# Unified Action Handlers (All Read json_mode from Context)
# =============================================================================

def handle_clone(
    ctx: Any,
    git_url: str,
    destination: Optional[str] = None,
    branch: Optional[str] = None,
    depth: Optional[int] = None,
    no_repair: bool = False
) -> None:
    """Clone a Git repository and automatically bootstrap/repair the Drift workspace."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        target_dir = Path(destination).resolve() if destination else None
        execute_clone(
            git_url=git_url,
            target_dir=target_dir,
            branch=branch,
            depth=depth,
            no_repair=no_repair,
            json_mode=cli_ctx.json_mode
        )


def handle_init(
    ctx: Any,
    force: bool = False
) -> None:
    """Initialize a new drift workspace."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = Path(cli_ctx.directory).resolve() if cli_ctx.directory else Path.cwd().resolve()
        execute_init(drift_root, force=force, no_git_root=cli_ctx.no_git_root, json_mode=cli_ctx.json_mode)
        if not cli_ctx.json_mode:
            cli_ctx.print_message("[bold yellow]✨[/bold yellow] [bold green]Initialized drift workspace![/bold green]", "✨ Initialized drift workspace!")
            cli_ctx.print_message("[bold yellow]📁[/bold yellow] [bold green]Created render/ sandbox Git database.[/bold green]", "📁 Created render/ sandbox Git database.")
            cli_ctx.print_message("[bold yellow]📁[/bold yellow] [bold green]Created install/ local state Git database.[/bold green]", "📁 Created install/ local state Git database.")
            cli_ctx.print_message("[bold yellow]📝[/bold yellow] [bold green]Generated drift.toml template.[/bold green]", "📝 Generated drift.toml template.")
            cli_ctx.print_message("[bold yellow]📝[/bold yellow] [bold green]Generated config/envsubst.bash, config/mustache.envst.json, and config/jinja2.mustache.json.[/bold green]", "📝 Generated config/envsubst.bash, config/mustache.envst.json, and config/jinja2.mustache.json.")


def handle_new(
    ctx: Any,
    package_name: str,
    force: bool = False,
    target: Optional[str] = None,
    method: Optional[str] = None
) -> None:
    """Scaffold a new package directory and drift_package.toml."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_new_package(
            drift_root,
            package_name,
            force=force,
            target_directory=target,
            install_method=method,
            json_mode=cli_ctx.json_mode
        )


def handle_add(
    ctx: Any,
    package_name: str,
    paths: List[str],
    dry_run: bool = False,
    no_hooks: bool = False
) -> None:
    """Import files or folders from the system into a package (with dot-prefix translation)."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_add(drift_root, package_name, paths, dry_run=dry_run, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_adopt(
    ctx: Any,
    packages: Optional[List[str]] = None,
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False,
    no_hooks: bool = False
) -> None:
    """Adopt active system drifts and incorporate them back into source templates."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_adopt(
            drift_root=drift_root,
            package_names=packages or [],
            interactive=interactive,
            accept_conflicts=accept_conflicts,
            force=force,
            dry_run=dry_run,
            json_mode=cli_ctx.json_mode,
            no_hooks=no_hooks
        )


def handle_deploy(
    ctx: Any,
    packages: Optional[List[str]] = None,
    force: bool = False,
    no_hooks: bool = False
) -> None:
    """Sandbox-compiles, stages, and deploys declarative configuration templates to target hosts."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_deploy(drift_root, packages, force=force, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_health(
    ctx: Any,
    packages: Optional[List[str]] = None,
    timeout: Optional[int] = None,
    verbose: bool = False
) -> None:
    """Run runtime health check probes on installed packages."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_health(drift_root, packages, json_mode=cli_ctx.json_mode, verbose=verbose, timeout=timeout)


def handle_uninstall(
    ctx: Any,
    packages: List[str],
    force: bool = False,
    dry_run: bool = False,
    detach: bool = False,
    no_hooks: bool = False
) -> None:
    """Uninstall a package from the system and restore any backups."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_uninstall(drift_root, packages, force=force, dry_run=dry_run, detach=detach, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_rollback(
    ctx: Any,
    packages: Optional[List[str]] = None,
    force: bool = False,
    no_hooks: bool = False
) -> None:
    """Rollback failed deployments and restore systems to the last committed clean state."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_rollback(drift_root, packages, force=force, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_status(
    ctx: Any,
    packages: Optional[List[str]] = None
) -> None:
    """Audit and aggregate configuration status across active packages."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_status(drift_root, packages, json_mode=cli_ctx.json_mode)


def handle_diff(
    ctx: Any,
    packages: Optional[List[str]] = None,
    template: bool = False,
    system: bool = False,
    side_by_side: bool = False,
    stat: bool = False
) -> None:
    """Visualize changes between configuration layers (Default: Pending Delta / Diff Δ)."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        diff_type = DiffType.PENDING
        if template:
            diff_type = DiffType.TEMPLATE
        elif system:
            diff_type = DiffType.SYSTEM
        execute_diff(drift_root, packages, diff_type=diff_type, side_by_side=side_by_side, stat=stat, json_mode=cli_ctx.json_mode)


def handle_gc(
    ctx: Any,
    dry_run: bool = False,
    no_hooks: bool = False
) -> None:
    """Identify and uninstall orphan packages (present in state but disabled in config)."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_gc(drift_root, dry_run=dry_run, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_repair(
    ctx: Any,
    dry_run: bool = False
) -> None:
    """Repair missing, damaged, or partially-initialized components in the drift workspace."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_repair(drift_root, dry_run=dry_run, json_mode=cli_ctx.json_mode)


def handle_help(
    ctx: Optional[Any] = None,
    topic: Optional[str] = None
) -> None:
    """Show overall model of drift and its detailed manual pages."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=False, use_rich=cli_ctx.use_rich):
        execute_help(topic)


def handle_complete(
    ctx: Optional[Any] = None,
    shell: Optional[str] = None
) -> None:
    """Generate interactive shell tab-completion scripts for bash, zsh, or fish."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        execute_complete(shell=shell, json_mode=cli_ctx.json_mode)


def handle_reverse_sync(
    ctx: Any,
    packages: Optional[List[str]] = None
) -> None:
    """(Low-Level) Synchronize changes from host system back to install/ state database."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_reverse_sync(drift_root, packages, json_mode=cli_ctx.json_mode)
        if not cli_ctx.json_mode:
            if packages:
                pkgs_str = ", ".join(packages)
                cli_ctx.print_message(f"[bold yellow]✨[/bold yellow] [bold green]Successfully reverse-synced package(s) '{pkgs_str}'![/bold green]", f"✨ Successfully reverse-synced package(s) '{pkgs_str}'!")
            else:
                cli_ctx.print_message("[bold yellow]✨[/bold yellow] [bold green]Successfully reverse-synced all enabled packages![/bold green]", "✨ Successfully reverse-synced all enabled packages!")


def handle_render(
    ctx: Any,
    packages: Optional[List[str]] = None,
    no_hooks: bool = False
) -> None:
    """(Low-Level) Render templates of a package or all enabled packages."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_render(drift_root, packages, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)
        if not cli_ctx.json_mode:
            if packages:
                pkgs_str = ", ".join(packages)
                cli_ctx.print_message(f"[bold yellow]✨[/bold yellow] [bold green]Successfully rendered package(s) '{pkgs_str}'![/bold green]", f"✨ Successfully rendered package(s) '{pkgs_str}'!")
            else:
                cli_ctx.print_message("[bold yellow]✨[/bold yellow] [bold green]Successfully rendered all enabled packages![/bold green]", "✨ Successfully rendered all enabled packages!")


def handle_render_commit(
    ctx: Any,
    message: str = "",
    packages: Optional[List[str]] = None
) -> None:
    """(Low-Level) Stage and commit compiled render sandbox changes."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=False, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_render_commit(drift_root, message, packages)


def handle_stage(
    ctx: Any,
    packages: Optional[List[str]] = None,
    force: bool = False
) -> None:
    """(Low-Level) Stage compiled sandbox templates from render/ to install/ state database."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_stage(drift_root, packages, force=force, json_mode=cli_ctx.json_mode)


def handle_apply(
    ctx: Any,
    packages: Optional[List[str]] = None,
    force: bool = False,
    no_hooks: bool = False
) -> None:
    """(Low-Level) Apply configurations from state database to active host system."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_apply(drift_root, packages, force=force, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_install_commit(
    ctx: Any,
    message: str = "",
    packages: Optional[List[str]] = None
) -> None:
    """(Low-Level) Stage and commit install state directory changes."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=False, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_install_commit(drift_root, message, packages)


def handle_hook(
    ctx: Any,
    package: str,
    hook_name: str
) -> None:
    """(Low-Level) Trigger a specific lifecycle hook script for a single package."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich):
        drift_root = cli_ctx.get_drift_root()
        execute_hook(drift_root, package, hook_name, json_mode=cli_ctx.json_mode)


CLI_HANDLERS: Dict[str, Callable[..., Any]] = {
    "clone": handle_clone,
    "init": handle_init,
    "new": handle_new,
    "add": handle_add,
    "adopt": handle_adopt,
    "deploy": handle_deploy,
    "health": handle_health,
    "uninstall": handle_uninstall,
    "rollback": handle_rollback,
    "status": handle_status,
    "diff": handle_diff,
    "gc": handle_gc,
    "repair": handle_repair,
    "help": handle_help,
    "complete": handle_complete,
    "reverse-sync": handle_reverse_sync,
    "render": handle_render,
    "render-commit": handle_render_commit,
    "stage": handle_stage,
    "apply": handle_apply,
    "install-commit": handle_install_commit,
    "hook": handle_hook,
}
