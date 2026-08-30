import argparse
from typing import Optional, List, Dict, Any
from pathlib import Path

from .schema import (
    CompletionSchema,
    OptionSpec,
    build_completion_schema,
    is_movable_global_option,
)
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
    execute_complete,
)


def generate_argparse_parser(schema: CompletionSchema) -> argparse.ArgumentParser:
    """Constructs an argparse.ArgumentParser programmatically from a CompletionSchema."""
    parser = argparse.ArgumentParser(description=schema.description)

    # 1. Global options on root parser
    for opt in schema.global_options:
        _add_option_to_parser(parser, opt)

    # 2. Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    for cmd_name, cmd in schema.commands.items():
        cmd_parser = subparsers.add_parser(cmd.name, help=cmd.description)

        # Positional arguments
        for pos in cmd.positionals:
            nargs = pos.nargs
            if nargs is None:
                if pos.repeatable:
                    nargs = "+" if pos.required else "*"
                elif not pos.required:
                    nargs = "?"

            kwargs: Dict[str, Any] = {"help": pos.description}
            if nargs is not None:
                kwargs["nargs"] = nargs
            if pos.default is not None:
                kwargs["default"] = pos.default

            cmd_parser.add_argument(pos.name, **kwargs)

        # Command Options (handling mutually exclusive groups)
        mutex_groups: Dict[str, Any] = {}
        for opt in cmd.options:
            target_parser = cmd_parser
            if opt.mutex_group:
                if opt.mutex_group not in mutex_groups:
                    mutex_groups[opt.mutex_group] = cmd_parser.add_mutually_exclusive_group()
                target_parser = mutex_groups[opt.mutex_group]
            _add_option_to_parser(target_parser, opt)

        # Attach movable global options (like --json, -v/--verbose) to subparsers with SUPPRESS default
        existing_flags = {flag for option in cmd.options for flag in option.flags}
        for opt in schema.global_options:
            if not is_movable_global_option(opt):
                continue
            if any(f in existing_flags for f in opt.flags):
                continue
            _add_option_to_parser(cmd_parser, opt, default_override=argparse.SUPPRESS)

    return parser


def _add_option_to_parser(target_parser: Any, opt: OptionSpec, default_override: Any = None) -> None:
    """Helper to attach an OptionSpec to an ArgumentParser or MutuallyExclusiveGroup."""
    kwargs: Dict[str, Any] = {"help": opt.description}
    if opt.dest:
        kwargs["dest"] = opt.dest
    if opt.action:
        kwargs["action"] = opt.action
    elif not opt.takes_value:
        kwargs["action"] = "store_true"

    if default_override is not None:
        kwargs["default"] = default_override
    elif opt.default is not None:
        kwargs["default"] = opt.default

    if opt.takes_value:
        if opt.type is not None:
            kwargs["type"] = opt.type
        if opt.required:
            kwargs["required"] = True

    target_parser.add_argument(*opt.flags, **kwargs)


def make_parser() -> argparse.ArgumentParser:
    """Constructs the argparse ArgumentParser dynamically from the CLI schema."""
    schema = build_completion_schema()
    return generate_argparse_parser(schema)


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

    global_keys = {"command", "no_git_root", "verbose", "json", "directory"}
    kwargs = {k: v for k, v in vars(args).items() if k not in global_keys}
    handler(cli_ctx, **kwargs)
