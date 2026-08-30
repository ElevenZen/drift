import argparse
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Dict, Any, Callable


class SourceType(Enum):
    """Specifies the data source for resolving argument completion candidates."""
    NONE = auto()             # Free-text or no completions
    DYNAMIC_PACKAGES = auto() # Evaluates package folders dynamically (e.g., `ls src/`)
    FIXED_CHOICES = auto()    # Set of predefined static Choice items
    FILES = auto()            # Filesystem files
    DIRECTORIES = auto()      # Filesystem directories


@dataclass(frozen=True)
class Choice:
    """Represents a discrete completion option with an interactive description hint."""
    value: str
    description: str


@dataclass
class OptionSpec:
    """Specification for a command option flag."""
    flags: List[str]                      # e.g., ["-m", "--method"]
    description: str                      # Help / hint text for interactive menus and help output
    takes_value: bool = False             # Whether the option expects an argument
    dest: Optional[str] = None            # Explicit attribute name in parsed namespace
    default: Any = None                   # Default value if not provided
    type: Optional[Any] = None            # Type converter (e.g. int)
    required: bool = False                # Whether this option flag is required
    action: Optional[str] = None          # Custom argparse action (e.g. "store_true")
    choices: Optional[List[Choice]] = None # Predefined allowed values for this option
    is_directory: bool = False            # Whether the argument completes directories
    is_file: bool = False                 # Whether the argument completes files
    mutex_group: Optional[str] = None     # Name of mutually exclusive group if applicable


@dataclass
class PositionalSpec:
    """Specification for positional arguments of a command."""
    name: str                             # e.g., "package", "hook_name", "paths"
    description: str                      # Help / hint text for interactive menus and help output
    source_type: SourceType = SourceType.NONE
    choices: Optional[List[Choice]] = None # Choices when source_type == FIXED_CHOICES
    nargs: Optional[str] = None           # Explicit nargs override (e.g. "?", "*", "+")
    default: Any = None                   # Default value if optional
    repeatable: bool = False              # True if accepts 1 or more / 0 or more arguments
    required: bool = True                 # True if at least 1 positional argument is mandatory
    allow_free_text: bool = False         # True if accepts free text or existing packages (e.g. `new`)


@dataclass
class CommandSpec:
    """Complete specification of a subcommand."""
    name: str
    description: str
    positionals: List[PositionalSpec] = field(default_factory=list)
    options: List[OptionSpec] = field(default_factory=list)


@dataclass
class CompletionSchema:
    """Root completion schema representing the entire CLI application."""
    cli_name: str
    description: str
    global_options: List[OptionSpec]
    commands: Dict[str, CommandSpec]

    def build_argparse_parser(self) -> argparse.ArgumentParser:
        """Generates an argparse.ArgumentParser directly from this schema."""
        return generate_argparse_parser(self)

    def build_typer_app(
        self,
        handlers: Dict[str, Any],
        callback: Optional[Any] = None,
        app: Optional[Any] = None
    ) -> Any:
        """Generates and registers commands on a Typer app instance directly from this schema."""
        return generate_typer_app(self, handlers=handlers, callback_handler=callback, app=app)


# =============================================================================
# Predefined Choice Registries with Documentation Hints
# =============================================================================

LIFECYCLE_HOOKS: List[Choice] = [
    Choice("pre_source", "Run before reading source templates (CWD: src/<pkg>)"),
    Choice("post_render", "Run after sandbox compilation (CWD: render/<pkg>)"),
    Choice("pre_install", "Run before first-time installation (CWD: install/<pkg>)"),
    Choice("post_install", "Run after successful first-time installation (CWD: target_dir)"),
    Choice("pre_update", "Run before update deployment (CWD: install/<pkg>)"),
    Choice("post_update", "Run after successful update deployment (CWD: target_dir)"),
    Choice("pre_uninstall", "Run before uninstallation (CWD: install/<pkg>)"),
    Choice("post_uninstall", "Run after uninstallation (CWD: target_dir)"),
    Choice("health", "Run runtime health check probe (CWD: target_dir)"),
]

HELP_TOPICS: List[Choice] = [
    Choice("overall", "High-level architectural model and loop overview"),
    Choice("package", "Package structure, directory layouts, and configuration"),
    Choice("src", "Declarative source templates and dot-prefix rules"),
    Choice("render", "Sandbox rendering zone, compilers, and DAG pipelines"),
    Choice("install", "State database mechanics, delta staging, and collision guards"),
    Choice("fcd", "Fully-Controlled Directories, wild file tracking, and adoption"),
    Choice("ignore", "PCRE ignore pattern rules and .drift_ignore mechanics"),
    Choice("drift_package.toml", "Package-level configuration reference"),
    Choice("drift.toml", "Workspace-level configuration reference"),
    Choice("workspace", "Multi-machine workflows, host profiling, and local overrides"),
    Choice("health", "Package runtime health check probes and hooks"),
    Choice("clone", "Workspace cloning, bootstrap self-healing, and legacy migration"),
    Choice("faq", "Frequently asked questions and troubleshooting guides"),
]

INSTALL_METHODS: List[Choice] = [
    Choice("stow", "Symlink-based deployment (ideal for user dotfiles)"),
    Choice("copy", "Direct physical copy deployment (ideal for system configs)"),
]

SHELLS: List[Choice] = [
    Choice("bash", "GNU Bourne-Again Shell completion script"),
    Choice("zsh", "Z Shell completion script with rich descriptions"),
    Choice("fish", "Fish Shell declarative completion script"),
]

GLOBAL_OPTIONS: List[OptionSpec] = [
    OptionSpec(
        flags=["-C", "--directory"],
        description="Run as if drift was started in <directory> instead of current working directory",
        takes_value=True,
        is_directory=True
    ),
    OptionSpec(
        flags=["--no-git-root"],
        description="Stop resolving git root of cwd or -C directory, using the literal path instead"
    ),
    OptionSpec(
        flags=["-v", "--verbose"],
        description="Enable verbose (DEBUG) logging output"
    ),
    OptionSpec(
        flags=["--json"],
        description="Output results in structured machine-readable JSON format"
    ),
]

# =============================================================================
# Movable Global Flags
# =============================================================================
# Global flags that can be specified anywhere on the command line:
# either before the subcommand (e.g. `drift -v deploy`) or after the subcommand
# (e.g. `drift deploy -v`, `drift deploy --json`).
MOVABLE_GLOBAL_FLAGS: List[str] = [
    "--json",
    "-v",
    "--verbose",
]


def is_movable_global_option(opt: OptionSpec) -> bool:
    """Returns True if any flag of the option is designated as a movable global flag."""
    return any(flag in MOVABLE_GLOBAL_FLAGS for flag in opt.flags)


# =============================================================================
# Schema Factory
# =============================================================================

def build_completion_schema() -> CompletionSchema:
    """Builds and returns the authoritative Drift completion schema."""
    return CompletionSchema(
        cli_name="drift",
        description="drift: Decoupled Two-Stage Git-Backed Dotfiles Manager",
        global_options=GLOBAL_OPTIONS,
        commands={
            # =================================================================
            # High-Level User Commands (Ordered by Lifecycle)
            # =================================================================
            "clone": CommandSpec(
                name="clone",
                description="Clone a Git repository and automatically bootstrap/repair the Drift workspace",
                positionals=[
                    PositionalSpec(
                        name="git_url",
                        description="Remote or local Git repository URL/path",
                        source_type=SourceType.NONE,
                        required=True
                    ),
                    PositionalSpec(
                        name="destination",
                        description="Optional destination directory for the clone (defaults to repo name)",
                        source_type=SourceType.DIRECTORIES,
                        nargs="?",
                        default=None,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(["-b", "--branch"], "Specific branch to clone", takes_value=True, default=None),
                    OptionSpec(
                        flags=["--depth"],
                        description="Create a shallow clone with a history truncated to the specified number of commits",
                        takes_value=True,
                        type=int,
                        default=None
                    ),
                    OptionSpec(
                        flags=["--no-repair"],
                        description="Skip automatic workspace database and repository repair after cloning"
                    ),
                ]
            ),
            "init": CommandSpec(
                name="init",
                description="Initialize a new drift workspace",
                positionals=[],
                options=[
                    OptionSpec(["-f", "--force"], "Force re-initialization and overwrite existing files"),
                ]
            ),
            "new": CommandSpec(
                name="new",
                description="Scaffold a new package directory and drift_package.toml",
                positionals=[
                    PositionalSpec(
                        name="package_name",
                        description="Name of the new package to create",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        required=True,
                        allow_free_text=True
                    )
                ],
                options=[
                    OptionSpec(["-f", "--force"], "Forcefully overwrite any existing config file inside the package"),
                    OptionSpec(
                        flags=["-t", "--target"],
                        description="Explicitly configure the deployment target directory inside drift_package.toml",
                        dest="target",
                        takes_value=True,
                        is_directory=True
                    ),
                    OptionSpec(
                        flags=["-m", "--method"],
                        description="Explicitly configure the installation method ('stow' or 'copy') inside drift_package.toml",
                        dest="method",
                        takes_value=True,
                        choices=INSTALL_METHODS
                    ),
                ]
            ),
            "add": CommandSpec(
                name="add",
                description="Import files or folders from the system into a package (with dot-prefix translation)",
                positionals=[
                    PositionalSpec(
                        name="package_name",
                        description="Name of the package to add resources into",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        required=True
                    ),
                    PositionalSpec(
                        name="paths",
                        description="One or more file/folder paths to import",
                        source_type=SourceType.FILES,
                        nargs="+",
                        repeatable=True,
                        required=True
                    )
                ],
                options=[
                    OptionSpec(["--dry-run"], "Preview the import without making changes"),
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "adopt": CommandSpec(
                name="adopt",
                description="Adopt active system drifts and incorporate them back into source templates",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to adopt specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(["-i", "--interactive"], "Interactively reconcile each drifted file"),
                    OptionSpec(
                        flags=["--accept-conflicts"],
                        description="Apply conflicting patches, writing merge conflict markers directly into templates"
                    ),
                    OptionSpec(
                        flags=["-f", "--force"],
                        description="Force adoption even if the package source directory has uncommitted modifications"
                    ),
                    OptionSpec(
                        flags=["--dry-run"],
                        description="Simulate the adoption, previewing changes and conflict results"
                    ),
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "deploy": CommandSpec(
                name="deploy",
                description="Sandbox-compiles, stages, and deploys declarative configuration templates to target hosts.",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional specific package(s) to deploy",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-f", "--force"],
                        description="Forcefully deploy and bypass system drift sentinel safeguards"
                    ),
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "health": CommandSpec(
                name="health",
                description="Run runtime health check probes on installed packages",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to check health specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-t", "--timeout"],
                        description="Custom execution timeout in seconds per probe (default: package hook timeout or 120s)",
                        takes_value=True,
                        type=int,
                        default=None
                    ),
                    OptionSpec(
                        flags=["-v", "--verbose"],
                        description="Enable verbose output with probe stdout/stderr"
                    ),
                ]
            ),
            "uninstall": CommandSpec(
                name="uninstall",
                description="Uninstall a package from the system and restore any backups",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="One or more package names to uninstall",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="+",
                        repeatable=True,
                        required=True
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-f", "--force"],
                        description="Force uninstallation even if package is still active in drift.toml"
                    ),
                    OptionSpec(["--dry-run"], "Preview uninstallation without making changes"),
                    OptionSpec(
                        flags=["--detach"],
                        description="Remove management relationship but keep configurations as actual physical files on host system"
                    ),
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "rollback": CommandSpec(
                name="rollback",
                description="Rollback failed deployments and restore systems to the last committed clean state.",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to rollback specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-f", "--force"],
                        description="Force the rollback and skip failed interlock checking"
                    ),
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "status": CommandSpec(
                name="status",
                description="Audit and aggregate configuration status across active packages",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to audit specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[]
            ),
            "diff": CommandSpec(
                name="diff",
                description="Visualize changes between configuration layers (Default: Pending Delta / Diff Δ)",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to diff specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-t", "--template"],
                        description="Visualize Template Evolution (Diff A: src -> render)",
                        mutex_group="diff_mode"
                    ),
                    OptionSpec(
                        flags=["-s", "--system"],
                        description="Visualize System Drift (Diff B: System -> install)",
                        mutex_group="diff_mode"
                    ),
                    OptionSpec(
                        flags=["-y", "--side-by-side"],
                        description="Show side-by-side comparison (Note: relies on git/diff capabilities)"
                    ),
                    OptionSpec(
                        flags=["--stat"],
                        description="Show concise summary of changes (diffstat)"
                    ),
                ]
            ),
            "gc": CommandSpec(
                name="gc",
                description="Identify and uninstall orphan packages (present in state but disabled in config)",
                positionals=[],
                options=[
                    OptionSpec(["--dry-run"], "Simulate the garbage collection without making changes"),
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "repair": CommandSpec(
                name="repair",
                description="Repair missing, damaged, or partially-initialized components in the drift workspace.",
                positionals=[],
                options=[
                    OptionSpec(["--dry-run"], "Show repair actions without executing them"),
                ]
            ),
            "help": CommandSpec(
                name="help",
                description="Show overall model of drift and its detailed manual pages.",
                positionals=[
                    PositionalSpec(
                        name="topic",
                        description="Specific topic to display (package, src, render, install, drift_package.toml, drift.toml, workspace, health, clone)",
                        source_type=SourceType.FIXED_CHOICES,
                        choices=HELP_TOPICS,
                        nargs="?",
                        required=False
                    )
                ],
                options=[]
            ),

            # =================================================================
            # Low-Level Control Commands (Ordered by Pipeline Lifecycle)
            # =================================================================
            "reverse-sync": CommandSpec(
                name="reverse-sync",
                description="(Low-Level) Synchronize changes from host system back to install/ state database",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to reverse-sync specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[]
            ),
            "render": CommandSpec(
                name="render",
                description="(Low-Level) Render templates of a package or all enabled packages",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to render specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "render-commit": CommandSpec(
                name="render-commit",
                description="(Low-Level) Stage and commit compiled render sandbox changes",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to commit specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-m", "--message"],
                        description="Commit message",
                        takes_value=True,
                        required=True
                    ),
                ]
            ),
            "stage": CommandSpec(
                name="stage",
                description="(Low-Level) Stage compiled sandbox templates from render/ to install/ state database",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to stage specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-f", "--force"],
                        description="Force staging and bypass uncommitted modifications check"
                    ),
                ]
            ),
            "apply": CommandSpec(
                name="apply",
                description="(Low-Level) Apply configurations from state database to active host system",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to apply specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-f", "--force"],
                        description="Force deployment and bypass check"
                    ),
                    OptionSpec(
                        flags=["--no-hooks", "--no-hook"],
                        description="Bypass and do not execute package lifecycle hooks",
                        dest="no_hooks"
                    ),
                ]
            ),
            "install-commit": CommandSpec(
                name="install-commit",
                description="(Low-Level) Stage and commit install state directory changes",
                positionals=[
                    PositionalSpec(
                        name="packages",
                        description="Optional package name(s) to commit specifically",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        nargs="*",
                        repeatable=True,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["-m", "--message"],
                        description="Commit message",
                        takes_value=True,
                        required=True
                    ),
                ]
            ),
            "hook": CommandSpec(
                name="hook",
                description="(Low-Level) Trigger a specific lifecycle hook script for a single package",
                positionals=[
                    PositionalSpec(
                        name="package",
                        description="Package name to trigger hook for",
                        source_type=SourceType.DYNAMIC_PACKAGES,
                        required=True
                    ),
                    PositionalSpec(
                        name="hook_name",
                        description="Name of the lifecycle hook to execute",
                        source_type=SourceType.FIXED_CHOICES,
                        choices=LIFECYCLE_HOOKS,
                        required=True
                    )
                ],
                options=[]
            ),
        }
    )


# =============================================================================
# Argparse Parser Generator
# =============================================================================

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

        # Attach movable global options (like --json, -v/--verbose) to subparsers with SUPPRESS default if not already present on subparser
        existing_flags = {flag for option in cmd.options for flag in option.flags}
        for opt in schema.global_options:
            if not is_movable_global_option(opt):
                continue
            if any(f in existing_flags for f in opt.flags):
                continue
            _add_option_to_parser(cmd_parser, opt, default_override=argparse.SUPPRESS)

    return parser


def _add_option_to_parser(target_parser, opt: OptionSpec, default_override: Any = None) -> None:
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
) -> Any:
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
    import typer

    # 1. Instantiate the root Typer application if not provided
    if app is None:
        app = typer.Typer(
            help=schema.description,
            add_completion=False,
            no_args_is_help=True
        )

    # 2. Wrap and attach the main root callback (global flags: -C, --no-git-root, -v, --json)
    if callback_handler is not None:
        callback_wrapper = _create_typer_callback_wrapper(schema.global_options, callback_handler)
        app.callback()(callback_wrapper)

    # 3. Wrap and attach each subcommand dynamically from the schema
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
    import typer
    import inspect

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
    import typer
    import inspect

    # params simulates the parameters of function decorated by typer.Command.
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
        # list all flag names in this command.
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
        # because this verbose hasn't been read in main_callback() .
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

