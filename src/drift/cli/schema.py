from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Dict, Any


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
    Choice("pre_uninstall", "Run before uninstallation (CWD: target_dir)"),
    Choice("post_uninstall", "Run after uninstallation (CWD: install/<pkg>)"),
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

PACKAGE_STAGES: List[Choice] = [
    Choice("install", "Read health hook from install/ directory (default)"),
    Choice("source", "Read and render health hook from src/ directory"),
]

SHELLS: List[Choice] = [
    Choice("bash", "GNU Bourne-Again Shell completion script"),
    Choice("zsh", "Z Shell completion script with rich descriptions"),
    Choice("fish", "Fish Shell declarative completion script"),
    Choice("nu", "Nushell custom completion script"),
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
                        flags=["--from-stage", "--from"],
                        dest="from_stage",
                        description="Directory base to load health probe from ('install' or 'source')",
                        takes_value=True,
                        type=str,
                        choices=PACKAGE_STAGES,
                        default="install"
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
            "complete": CommandSpec(
                name="complete",
                description="Generate interactive shell tab-completion scripts for bash, zsh, or fish",
                positionals=[
                    PositionalSpec(
                        name="shell",
                        description="Target shell (bash, zsh, or fish). If omitted, automatically detects active shell from $SHELL",
                        source_type=SourceType.FIXED_CHOICES,
                        choices=SHELLS,
                        nargs="?",
                        default=None,
                        required=False
                    )
                ],
                options=[
                    OptionSpec(
                        flags=["--install", "-i"],
                        description="Install completion script directly into standard user completion directory on disk",
                        action="store_true"
                    )
                ]
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
                options=[
                    OptionSpec(
                        flags=["--from-stage", "--from"],
                        dest="from_stage",
                        description="Directory base to load hook from ('install' or 'source')",
                        takes_value=True,
                        type=str,
                        choices=PACKAGE_STAGES,
                        default=None
                    ),
                ]
            ),
        }
    )
