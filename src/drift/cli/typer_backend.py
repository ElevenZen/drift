import inspect
from typing import Optional, List, Dict, Any
import typer

from .schema import (
    CompletionSchema,
    CommandSpec,
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
)


# =============================================================================
# Typer App Generator (Metaprogramming Engine)
# =============================================================================
#
# Background & Architectural Rationale:
# -------------------------------------
# Typer (built on top of Click) relies heavily on Python function signature
# introspection (`inspect.signature`, `__annotations__`, and parameter defaults)
# to construct its CLI command tree, option flags, argument types, and help pages.
#
# Traditional Typer applications require defining verbose, decorated functions
# with explicit `typer.Option(...)` and `typer.Argument(...)` default values in
# every parameter definition. To maintain a Single Source of Truth (SSOT) across
# both CLI backends (`typer` and `argparse`) and shell tab-completion engines,
# this module programmatically synthesizes Typer-compatible function wrappers
# at runtime directly from `CompletionSchema`, `CommandSpec`, and `OptionSpec`.
#
# Key Metaprogramming Mechanics:
# 1. Parameter Synthesis: Constructs `inspect.Parameter` objects with typed annotations
#    (`str`, `List[str]`, `bool`, `Optional[int]`, etc.) and default values
#    (`typer.Argument(...)` and `typer.Option(...)`).
# 2. Signature Injection: Assigns `__signature__` and `__annotations__` to wrapper
#    functions so Typer/Click introspects the dynamic schema as if it were hardcoded.
# 3. Dynamic Dispatch: At invocation time, resolves context state (`DriftCLIContext`),
#    propagates global flags (`--json`), filters keyword arguments against the target
#    handler's actual parameters, and executes the plain Python handler.
# =============================================================================


def generate_typer_app(
    schema: CompletionSchema,
    handlers: Dict[str, Any],
    callback_handler: Optional[Any] = None,
    app: Optional[Any] = None
) -> typer.Typer:
    """Constructs and registers Typer commands programmatically from a CompletionSchema.

    Args:
        schema: The authoritative CompletionSchema defining commands, positionals, and options.
        handlers: Mapping of command names to clean, plain Python handler functions
                  (e.g., `{"deploy": handle_deploy, "init": handle_init}`).
        callback_handler: Optional main CLI initialization callback (e.g., `main_callback`).
        app: Existing `typer.Typer` instance, or None to instantiate a new one.

    Returns:
        The fully configured `typer.Typer` application instance ready for execution.
    """
    if app is None:
        app = typer.Typer(
            help=schema.description,
            add_completion=False,
            no_args_is_help=True
        )

    # 1. Wrap and attach the main root callback (global flags: -C, --no-git-root, -v, --json)
    if callback_handler is not None:
        callback_wrapper = _create_typer_callback_wrapper(schema.global_options, callback_handler)
        app.callback()(callback_wrapper)

    # 2. Wrap and attach each subcommand dynamically from the schema
    for cmd_name, cmd in schema.commands.items():
        if cmd_name not in handlers:
            continue
        handler_func = handlers[cmd_name]
        command_wrapper = _create_typer_command_wrapper(cmd, handler_func, schema.global_options)
        # Register on Typer app with explicit command name and help docstring
        app.command(cmd.name, help=cmd.description)(command_wrapper)

    return app


def _create_typer_callback_wrapper(global_options: List[OptionSpec], callback_func: Any) -> Any:
    """Builds a typed wrapper with typer.Option defaults for the main CLI callback.

    Inspects `global_options` and synthesizes parameters (e.g. `directory`, `no_git_root`,
    `verbose`, `json`) with proper flag aliases (`-C/--directory`, `-v/--verbose`) and
    help descriptions for the root CLI context.
    """
    params = []
    annotations = {}
    sig = inspect.signature(callback_func)

    # Inject typer.Context parameter if expected by the underlying callback function
    if "ctx" in sig.parameters:
        params.append(inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context))
        annotations["ctx"] = typer.Context

    # Synthesize typer.Option parameters for all global options
    for opt in global_options:
        dest = opt.dest
        if not dest:
            for f in opt.flags:
                if f.startswith("--"):
                    dest = f.lstrip("-").replace("-", "_")
                    break
            if not dest:
                dest = opt.flags[0].lstrip("-").replace("-", "_")

        # Sort flags with long flags first (e.g. ['--directory', '-C']) for Typer convention
        flags = sorted(opt.flags, key=lambda f: (not f.startswith("--"), f))

        if opt.takes_value:
            opt_type = opt.type or str
            param_type = Optional[opt_type]
            default_val = typer.Option(opt.default, *flags, help=opt.description)
        else:
            param_type = bool
            default_val = typer.Option(False, *flags, help=opt.description)

        params.append(inspect.Parameter(dest, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default_val, annotation=param_type))
        annotations[dest] = param_type

    def wrapper(*args, **kwargs):
        # Filter kwargs to only pass parameters explicitly accepted by callback_func
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return callback_func(*args, **filtered_kwargs)

    # Attach synthetic signature and metadata for Typer introspection
    wrapper.__name__ = callback_func.__name__
    wrapper.__doc__ = callback_func.__doc__
    wrapper.__signature__ = inspect.Signature(params)
    wrapper.__annotations__ = annotations
    return wrapper


def _create_typer_command_wrapper(
    cmd: CommandSpec,
    target_func: Any,
    global_options: Optional[List[OptionSpec]] = None
) -> Any:
    """Builds a dynamic typed wrapper with typer.Argument and typer.Option defaults for a subcommand.

    Metaprogramming Pipeline:
    1. Context Parameter Injection: Checks if `target_func` expects `ctx` and adds `ctx: typer.Context`.
    2. Positional Argument Translation: Maps `PositionalSpec` into `typer.Argument(...)` with
       correct cardinality types (`str`, `Optional[str]`, `List[str]`, `Optional[List[str]]`).
    3. Option Flag Translation: Maps command-specific `OptionSpec` and global flags (e.g., `--json`)
       into `typer.Option(...)` with boolean or typed value defaults.
    4. Function Signature Construction: Assembles an `inspect.Signature` object with all parameters
       and attaches it to `wrapper.__signature__` and `wrapper.__annotations__`.
    5. Runtime Invocation: Intercepts CLI arguments, resolves context state (`DriftCLIContext`),
       propagates `--json` flags, filters parameters, and executes `target_func`.
    """
    params = []
    annotations = {}
    sig = inspect.signature(target_func)

    # -------------------------------------------------------------------------
    # Step 1: Inject CLI context parameter (ctx: typer.Context)
    # -------------------------------------------------------------------------
    if "ctx" in sig.parameters:
        params.append(inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context))
        annotations["ctx"] = typer.Context

    # -------------------------------------------------------------------------
    # Step 2: Build positional arguments (typer.Argument)
    # -------------------------------------------------------------------------
    for pos in cmd.positionals:
        name = pos.name
        help_text = pos.description

        if pos.repeatable:
            # Repeatable argument: List[str] if required, Optional[List[str]] if optional
            if pos.required:
                param_type = List[str]
                default_val = typer.Argument(..., help=help_text)
            else:
                param_type = Optional[List[str]]
                default_val = typer.Argument(None, help=help_text)
        else:
            # Single argument: str if required, Optional[str] if optional
            if pos.required:
                param_type = str
                default_val = typer.Argument(..., help=help_text)
            else:
                param_type = Optional[str]
                default_val = typer.Argument(None, help=help_text)

        params.append(inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default_val, annotation=param_type))
        annotations[name] = param_type

    # -------------------------------------------------------------------------
    # Step 3: Collect options (Command Options + Movable Global Flags)
    # -------------------------------------------------------------------------
    all_options = list(cmd.options)

    # Allow movable global options (like --json, -v/--verbose) to be specified after subcommands
    if global_options:
        existing_flags = {flag for option in all_options for flag in option.flags}
        for g_opt in global_options:
            if is_movable_global_option(g_opt) and not any(f in existing_flags for f in g_opt.flags):
                all_options.append(g_opt)

    # -------------------------------------------------------------------------
    # Step 4: Build option parameters (typer.Option)
    # -------------------------------------------------------------------------
    for opt in all_options:
        dest = opt.dest
        if not dest:
            for f in opt.flags:
                if f.startswith("--"):
                    dest = f.lstrip("-").replace("-", "_")
                    break
            if not dest:
                dest = opt.flags[0].lstrip("-").replace("-", "_")

        # Sort long flags first so Typer registers the primary name accurately
        flags = sorted(opt.flags, key=lambda f: (not f.startswith("--"), f))
        help_text = opt.description

        if opt.takes_value:
            opt_type = opt.type or str
            if opt.required:
                param_type = opt_type
                default_val = typer.Option(..., *flags, help=help_text)
            else:
                param_type = Optional[opt_type]
                default_val = typer.Option(opt.default, *flags, help=help_text)
        else:
            param_type = bool
            default_val = typer.Option(False, *flags, help=help_text)

        params.append(inspect.Parameter(dest, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default_val, annotation=param_type))
        annotations[dest] = param_type

    # -------------------------------------------------------------------------
    # Step 5: Wrapper execution & runtime dispatch
    # -------------------------------------------------------------------------
    def wrapper(*args, **kwargs):
        # Extract CLI context from kwargs or args
        ctx = kwargs.get("ctx") or (args[0] if args and isinstance(args[0], typer.Context) else None)
        cli_ctx = getattr(ctx, "obj", None) if ctx else None

        # If --json was specified on the subcommand, update DriftCLIContext state
        if kwargs.get("json") and cli_ctx:
            cli_ctx.json_mode = True

        # If -v/--verbose was specified on the subcommand, enable debug logging
        if kwargs.get("verbose"):
            from . import setup_logging
            import logging
            setup_logging(level=logging.DEBUG)

        # Filter kwargs to match the handler's signature (e.g. excluding --json if handled via context)
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return target_func(*args, **filtered_kwargs)

    # -------------------------------------------------------------------------
    # Step 6: Attach synthetic signature & metadata for Typer introspection
    # -------------------------------------------------------------------------
    wrapper.__name__ = target_func.__name__
    wrapper.__doc__ = cmd.description
    wrapper.__signature__ = inspect.Signature(params)
    wrapper.__annotations__ = annotations
    return wrapper


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

app = generate_typer_app(
    schema=schema,
    handlers=CLI_HANDLERS,
    callback_handler=main_callback
)
