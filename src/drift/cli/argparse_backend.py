import argparse
from typing import Optional, List
from pathlib import Path

from .schema import build_completion_schema, generate_argparse_parser
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


def make_parser() -> argparse.ArgumentParser:
    """Constructs the argparse ArgumentParser dynamically from the CLI schema."""
    schema = build_completion_schema()
    return schema.build_argparse_parser()


def run_argparse_cli(argv=None) -> None:
    """Executes the CLI command using the argparse parser and unified CLI handlers."""
    schema = build_completion_schema()
    parser = make_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        from . import setup_logging
        import logging
        setup_logging(level=logging.DEBUG)

    if not args.command:
        parser.print_help()
        return

    cmd_spec = schema.commands.get(args.command)
    handler = CLI_HANDLERS.get(args.command)
    if not cmd_spec or not handler:
        parser.print_help()
        return

    cli_ctx = DriftCLIContext(
        directory=getattr(args, "directory", None),
        no_git_root=getattr(args, "no_git_root", False),
        json_mode=getattr(args, "json", False),
        use_rich=False
    )

    # Split args into global ones and command ones, the global ones are handled in Context object.
    global_keys = {"command", "no_git_root", "verbose", "json", "directory"}
    kwargs = {k: v for k, v in vars(args).items() if k not in global_keys}
    handler(cli_ctx, **kwargs)
