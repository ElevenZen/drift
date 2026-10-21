import re
from pathlib import Path
from typing import Optional, List, Any, Dict, Callable, Sequence

from .actions import (
    execute_render,
    execute_init,
    execute_stage,
    execute_render_commit,
    execute_apply,
    execute_install_commit,
    execute_reverse_sync,
    execute_new_package,
    execute_packages_enable,
    execute_uninstall,
    execute_status,
    execute_gc,
    execute_diff,
    execute_add,
    execute_adopt,
    execute_rollback,
    execute_deploy,
    execute_plan,
    execute_repair,
    execute_health,
    execute_clone,
    execute_help,
    execute_hook,
    execute_complete,
    execute_install_stub,
)
from ..core.constants import InstallMethod, PackageStage, RollbackPolicy
from ..models import WorkspaceDiffType
from ..primitives.deploy_repo import DeployOptions
from ..utils.workspace_root import resolve_drift_workspace_root
from .error_boundary import cli_error_boundary


class DriftCLIContext:
    """Unified CLI context helper holding configuration parameters and resolving directory root."""
    def __init__(
        self,
        directory: Optional[str] = None,
        json_mode: bool = False,
        use_rich: bool = True,
        raw_errors: bool = False,
        verbose: bool = False,
    ) -> None:
        self.directory: Optional[str] = directory
        self.json_mode: bool = json_mode
        self.use_rich: bool = use_rich
        self.raw_errors: bool = raw_errors
        self.verbose: bool = verbose

    def get_start_dir(self) -> Path:
        """Returns the absolute start directory (``-C/--directory`` or the current working directory)."""
        return Path(self.directory).resolve() if self.directory else Path.cwd().resolve()

    def get_drift_root(self) -> Path:
        """Resolves the drift workspace root: the nearest directory at or above the start directory
        that contains a workspace config marker under ``config/``."""
        return resolve_drift_workspace_root(self.get_start_dir())

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
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
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
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_start_dir()
        execute_init(drift_root, force=force, json_mode=cli_ctx.json_mode)
        if not cli_ctx.json_mode:
            cli_ctx.print_message("[bold yellow]✨[/bold yellow] [bold green]Initialized drift workspace![/bold green]", "✨ Initialized drift workspace!")
            cli_ctx.print_message("[bold yellow]📁[/bold yellow] [bold green]Created render/ sandbox Git database.[/bold green]", "📁 Created render/ sandbox Git database.")
            cli_ctx.print_message("[bold yellow]📁[/bold yellow] [bold green]Created install/ local state Git database.[/bold green]", "📁 Created install/ local state Git database.")
            cli_ctx.print_message("[bold yellow]📝[/bold yellow] [bold green]Generated drift_workspace.toml template.[/bold green]", "📝 Generated drift_workspace.toml template.")
            cli_ctx.print_message("[bold yellow]📝[/bold yellow] [bold green]Generated config/mustache.envst.json and config/jinja2.mustache.json.[/bold green]", "📝 Generated config/mustache.envst.json and config/jinja2.mustache.json.")


def handle_new(
    ctx: Any,
    package_name: str,
    force: bool = False,
    target: Optional[str] = None,
    method: Optional[str] = None
) -> None:
    """Scaffold a new package directory and drift_package.toml."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        target_path = Path(target) if target is not None else None
        resolved_method = InstallMethod.from_str(method) if method is not None else None
        drift_root = cli_ctx.get_drift_root()
        execute_new_package(
            drift_root,
            package_name,
            force=force,
            target_directory=target_path,
            install_method=resolved_method,
            json_mode=cli_ctx.json_mode
        )


def _handle_packages_enable(ctx: Any, packages: Sequence[str], enabled: bool, local: bool, dry_run: bool) -> None:
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_packages_enable(
            drift_root,
            list(packages or ()),
            enabled=enabled,
            local=local,
            dry_run=dry_run,
            json_mode=cli_ctx.json_mode,
            use_rich=cli_ctx.use_rich,
        )


def handle_enable(
    ctx: Any,
    packages: Sequence[str],
    local: bool = False,
    dry_run: bool = False,
) -> None:
    """Enable package(s) in [packages.enable] of drift_workspace.toml."""
    _handle_packages_enable(ctx, packages, enabled=True, local=local, dry_run=dry_run)


def handle_disable(
    ctx: Any,
    packages: Sequence[str],
    local: bool = False,
    dry_run: bool = False,
) -> None:
    """Disable package(s) in [packages.enable] of drift_workspace.toml (does not uninstall)."""
    _handle_packages_enable(ctx, packages, enabled=False, local=local, dry_run=dry_run)


def handle_add(
    ctx: Any,
    package_name: str,
    paths: List[str],
    dry_run: bool = False,
    no_hooks: bool = False
) -> None:
    """Import files or folders from the system into a package (with dot-prefix translation)."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        import_paths = [Path(p) for p in paths]
        execute_add(drift_root, package_name, import_paths, dry_run=dry_run, json_mode=cli_ctx.json_mode, no_hooks=no_hooks, verbose=cli_ctx.verbose)


def handle_adopt(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False,
    no_hooks: bool = False,
    no_render: bool = False,
    no_reverse_sync: bool = False,
) -> None:
    """Adopt active system drifts and incorporate them back into source templates."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_adopt(
            drift_root=drift_root,
            package_names=pkgs,
            interactive=interactive,
            accept_conflicts=accept_conflicts,
            force=force,
            dry_run=dry_run,
            json_mode=cli_ctx.json_mode,
            no_hooks=no_hooks,
            no_render=no_render,
            no_reverse_sync=no_reverse_sync,
            verbose=cli_ctx.verbose,
        )


def handle_deploy(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    force: bool = False,
    no_hooks: bool = False,
    reinstall: bool = False,
    no_deps: bool = False,
    no_cache: bool = False,
    dry_run: bool = False,
    rollback: Optional[str] = None,
) -> None:
    """Sandbox-compiles, stages, and deploys declarative configuration templates to target hosts."""
    if dry_run:
        handle_plan(
            ctx=ctx,
            packages=packages,
            force=force,
            reinstall=reinstall,
            no_hooks=no_hooks,
            with_hooks=False,
            no_deps=no_deps,
            no_cache=no_cache,
            show_all=False,
            command_name="deploy",
        )
        return

    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        resolved_rollback = RollbackPolicy.from_val(rollback) if rollback is not None else None
        drift_root = cli_ctx.get_drift_root()
        execute_deploy(
            drift_root,
            pkgs,
            force=force,
            json_mode=cli_ctx.json_mode,
            no_hooks=no_hooks,
            reinstall=reinstall,
            no_deps=no_deps,
            no_cache=no_cache,
            rollback=resolved_rollback,
        )


def handle_plan(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    force: bool = False,
    reinstall: bool = False,
    no_hooks: bool = False,
    with_hooks: bool = False,
    no_deps: bool = False,
    no_cache: bool = False,
    show_all: bool = False,
    command_name: str = "plan",
) -> None:
    """Simulates deployment planning and previews actions without modifying system state."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        if no_hooks and with_hooks:
            raise ValueError("Cannot specify both --no-hooks and --with-hooks.")
        options = DeployOptions(
            force=force,
            reinstall=reinstall,
            no_deps=no_deps,
            no_hooks=no_hooks,
            with_hooks=with_hooks,
            no_cache=no_cache,
            dry_run=True,
            show_all=show_all,
            verbose=cli_ctx.verbose,
        )
        drift_root = cli_ctx.get_drift_root()
        execute_plan(
            drift_root=drift_root,
            packages=pkgs,
            options=options,
            json_mode=cli_ctx.json_mode,
            verbose=cli_ctx.verbose,
            command_name=command_name,
        )


def handle_health(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    timeout: Optional[int] = None,
    from_stage: str = "install",
) -> None:
    """Run runtime health check probes on packages."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        resolved_stage = PackageStage.from_str(from_stage)
        drift_root = cli_ctx.get_drift_root()
        execute_health(drift_root, pkgs, json_mode=cli_ctx.json_mode, verbose=cli_ctx.verbose, timeout=timeout, from_stage=resolved_stage)


def handle_uninstall(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    force: bool = False,
    dry_run: bool = False,
    detach: bool = False,
    no_hooks: bool = False,
    no_deps: bool = False,
) -> None:
    """Uninstall a package from the system and restore any backups."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_uninstall(
            drift_root,
            pkgs,
            force=force,
            dry_run=dry_run,
            detach=detach,
            json_mode=cli_ctx.json_mode,
            no_hooks=no_hooks,
            no_deps=no_deps,
            verbose=cli_ctx.verbose,
        )


def handle_rollback(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    force: bool = False,
    no_hooks: bool = False
) -> None:
    """Rollback failed deployments and restore systems to the last committed clean state."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_rollback(drift_root, pkgs, force=force, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_status(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    list_only: bool = False
) -> None:
    """Audit and aggregate configuration status across active packages."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_status(drift_root, pkgs, list_only=list_only, json_mode=cli_ctx.json_mode)


def handle_diff(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    template: bool = False,
    system: bool = False,
    side_by_side: bool = False,
    stat: bool = False
) -> None:
    """Visualize changes between configuration layers (Default: Pending Delta / Diff Δ)."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        diff_type = WorkspaceDiffType.PENDING
        if template:
            diff_type = WorkspaceDiffType.TEMPLATE
        elif system:
            diff_type = WorkspaceDiffType.SYSTEM
        execute_diff(drift_root, pkgs, diff_type=diff_type, side_by_side=side_by_side, stat=stat, json_mode=cli_ctx.json_mode)


def handle_gc(
    ctx: Any,
    dry_run: bool = False,
    no_hooks: bool = False
) -> None:
    """Identify and uninstall orphan packages (present in state but disabled in config)."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_gc(drift_root, dry_run=dry_run, json_mode=cli_ctx.json_mode, no_hooks=no_hooks)


def handle_repair(
    ctx: Any,
    dry_run: bool = False
) -> None:
    """Repair missing, damaged, or partially-initialized components in the drift workspace."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_repair(drift_root, dry_run=dry_run, json_mode=cli_ctx.json_mode)


def handle_help(
    ctx: Optional[Any] = None,
    topic: Optional[str] = None
) -> None:
    """Show overall model of drift and its detailed manual pages."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=False, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        execute_help(topic)


def handle_complete(
    ctx: Optional[Any] = None,
    shell: Optional[str] = None,
    install: bool = False
) -> None:
    """Generate or install interactive shell tab-completion scripts for bash, zsh, or fish."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        execute_complete(shell=shell, install=install, json_mode=cli_ctx.json_mode)


def handle_reverse_sync(
    ctx: Any,
    packages: Optional[Sequence[str]] = None
) -> None:
    """(Low-Level) Synchronize changes from host system back to install/ state database."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_reverse_sync(drift_root, pkgs, json_mode=cli_ctx.json_mode)
        if not cli_ctx.json_mode:
            if pkgs:
                pkgs_str = ", ".join(pkgs)
                cli_ctx.print_message(f"[bold yellow]✨[/bold yellow] [bold green]Successfully reverse-synced package(s) '{pkgs_str}'![/bold green]", f"✨ Successfully reverse-synced package(s) '{pkgs_str}'!")
            else:
                cli_ctx.print_message("[bold yellow]✨[/bold yellow] [bold green]Successfully reverse-synced all enabled packages![/bold green]", "✨ Successfully reverse-synced all enabled packages!")


def handle_render(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    no_hooks: bool = False,
    no_cache: bool = False,
    dry_run: bool = False,
    with_hooks: bool = False,
) -> None:
    """(Low-Level) Render templates of a package or all enabled packages."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        if no_hooks and with_hooks:
            raise ValueError("Cannot specify both --no-hooks and --with-hooks.")
        drift_root = cli_ctx.get_drift_root()
        execute_render(
            drift_root,
            pkgs,
            json_mode=cli_ctx.json_mode,
            no_hooks=no_hooks,
            no_cache=no_cache,
            dry_run=dry_run,
            with_hooks=with_hooks,
            verbose=cli_ctx.verbose,
        )
        if not cli_ctx.json_mode:
            prefix = "[DRY-RUN] " if dry_run else ""
            if pkgs:
                pkgs_str = ", ".join(pkgs)
                cli_ctx.print_message(f"[bold yellow]✨[/bold yellow] [bold green]{prefix}Successfully rendered package(s) '{pkgs_str}'![/bold green]", f"✨ {prefix}Successfully rendered package(s) '{pkgs_str}'!")
            else:
                cli_ctx.print_message(f"[bold yellow]✨[/bold yellow] [bold green]{prefix}Successfully rendered all enabled packages![/bold green]", f"✨ {prefix}Successfully rendered all enabled packages!")


def handle_render_commit(
    ctx: Any,
    message: str = "",
    packages: Optional[Sequence[str]] = None
) -> None:
    """(Low-Level) Stage and commit compiled render sandbox changes."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=False, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_render_commit(drift_root, message, pkgs)


def handle_stage(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    force: bool = False,
    no_deps: bool = False,
    dry_run: bool = False,
) -> None:
    """(Low-Level) Stage compiled sandbox templates from render/ to install/ state database."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_stage(
            drift_root,
            pkgs,
            force=force,
            json_mode=cli_ctx.json_mode,
            no_deps=no_deps,
            dry_run=dry_run,
            verbose=cli_ctx.verbose,
        )


def handle_apply(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    force: bool = False,
    reinstall: bool = False,
    no_hooks: bool = False,
    no_deps: bool = False,
    dry_run: bool = False,
    rollback: Optional[str] = None,
) -> None:
    """(Low-Level) Apply configurations from state database to active host system."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        resolved_rollback = RollbackPolicy.from_val(rollback) if rollback is not None else None
        drift_root = cli_ctx.get_drift_root()
        execute_apply(
            drift_root,
            pkgs,
            force=force,
            reinstall=reinstall,
            json_mode=cli_ctx.json_mode,
            no_hooks=no_hooks,
            no_deps=no_deps,
            dry_run=dry_run,
            verbose=cli_ctx.verbose,
            rollback=resolved_rollback,
        )


def handle_install_commit(
    ctx: Any,
    message: str = "",
    packages: Optional[Sequence[str]] = None
) -> None:
    """(Low-Level) Stage and commit install state directory changes."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=False, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        drift_root = cli_ctx.get_drift_root()
        execute_install_commit(drift_root, message, pkgs)


def handle_hook(
    ctx: Any,
    package: str,
    hook_name: str,
    from_stage: Optional[str] = None
) -> None:
    """(Low-Level) Trigger a specific lifecycle hook script for a single package."""
    cli_ctx = _extract_cli_context(ctx)
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        resolved_stage = PackageStage.from_str(from_stage) if from_stage is not None else None
        drift_root = cli_ctx.get_drift_root()
        execute_hook(drift_root, package, hook_name, json_mode=cli_ctx.json_mode, from_stage=resolved_stage)


def handle_install(
    ctx: Any,
    packages: Optional[Sequence[str]] = None,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """(Didactic Stub) Intercepts 'drift install' and provides guidance."""
    cli_ctx = _extract_cli_context(ctx)
    pkgs = packages or ()
    with cli_error_boundary(json_mode=cli_ctx.json_mode, use_rich=cli_ctx.use_rich, raw_errors=cli_ctx.raw_errors):
        execute_install_stub(packages=pkgs, json_mode=cli_ctx.json_mode)


CLI_HANDLERS: Dict[str, Callable[..., Any]] = {
    "clone": handle_clone,
    "init": handle_init,
    "new": handle_new,
    "add": handle_add,
    "enable": handle_enable,
    "disable": handle_disable,
    "adopt": handle_adopt,
    "plan": handle_plan,
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
    "install": handle_install,
}
