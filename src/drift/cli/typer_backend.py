from typing import Optional
import typer

from .schema import build_completion_schema
from .cli_handlers import DriftCLIContext, CLI_HANDLERS
from .actions import (
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
)


def main_callback(
    ctx: typer.Context,
    directory: Optional[str] = None,
    no_git_root: bool = False,
    verbose: bool = False,
    json: bool = False
) -> None:
    """Main CLI context initialization callback."""
    if verbose:
        from . import setup_logging
        import logging
        setup_logging(level=logging.DEBUG)
    ctx.obj = DriftCLIContext(
        directory=directory,
        no_git_root=no_git_root,
        json_mode=json,
        use_rich=True
    )


schema = build_completion_schema()

app = schema.build_typer_app(
    handlers=CLI_HANDLERS,
    callback=main_callback
)
