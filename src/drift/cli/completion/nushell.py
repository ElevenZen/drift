"""Nushell Tab-Completion Script Generator for Drift CLI.

===============================================================================
How Nushell Completion Works:
===============================================================================
Nushell (nu) provides a modern, strongly-typed custom completion engine built
around `export extern` definitions and custom completer commands.

1. Custom Completer Functions (`def "nu-complete <name>" []`):
   - Custom completers return structured records containing `value` and `description`:
     `[ { value: "pkg1", description: "Drift package 'pkg1'" }, ... ]`
   - These records render interactive dropdown completion menus in the terminal.

2. Declarative `extern` Definitions:
   - External CLI tools are declared with `export extern "<command>" [ ... ]`.
   - Flags specify long and short names: `--force(-f)` or `--target(-t): path`.
   - Positional arguments specify types (`string`, `path`, `int`) and optionality:
     - Required: `pkg: string@"nu-complete drift-packages"`
     - Optional: `topic?: string@"nu-complete drift-help-topics"`
     - Variadic/Rest: `...packages: string@"nu-complete drift-packages"`

3. Loading in Nushell:
   Users source or import the module in `~/.config/nushell/config.nu`:
   `use ~/.config/nushell/completions/drift.nu *`

-------------------------------------------------------------------------------
Minimal Standalone Nushell Completion Example:
-------------------------------------------------------------------------------
```nu
# 1. Custom package completer
def "nu-complete drift-packages" [] {
  let root = ($env.PWD | path expand)
  let src_dir = ($root | path join "src")
  if ($src_dir | path exists) {
    ls $src_dir | where type == dir | get name | path basename | each { |pkg|
      { value: $pkg, description: $"Drift package '($pkg)'" }
    }
  } else {
    []
  }
}

# 2. Root command definition (named 'main' in module file drift.nu)
export extern "main" [
  --help(-h)                   # Display help message
  --version(-V)                # Display version information
  --json                       # Machine-readable JSON output
  --verbose(-v)                # Enable verbose logging
  --directory(-C): path        # Target workspace directory
]

# 3. Subcommand definitions
export extern "drift deploy" [
  --force(-f)                  # Overwrite uncommitted system drift and force deployment
  --no-hooks                   # Skip lifecycle hook scripts
  --json                       # Machine-readable JSON output
  ...packages: string@"nu-complete drift-packages" # Packages to deploy
]
```
===============================================================================
"""

from typing import List, Optional
from ..schema import (
    CompletionSchema,
    CommandSpec,
    OptionSpec,
    PositionalSpec,
    SourceType,
    Choice,
    is_movable_global_option,
    SHELLS,
    HELP_TOPICS,
    INSTALL_METHODS,
    LIFECYCLE_HOOKS,
)


class NushellGenerator:
    """Generates a native Nushell completion script for Drift CLI.

    Translates a declarative CompletionSchema into modular `export extern`
    commands and custom completers with rich inline documentation.
    """

    def __init__(self, schema: CompletionSchema) -> None:
        self.schema = schema

    def generate(self) -> str:
        """Compiles and returns the full Nushell completion script as a string."""
        cli = self.schema.cli_name

        lines: List[str] = [
            f"# Nushell completion script for {cli}",
            f"# Auto-generated from {cli}.cli.schema. DO NOT EDIT MANUALLY.",
            "",
            "# =============================================================================",
            "# Custom Completer Functions",
            "# =============================================================================",
            "",
            f'def "nu-complete {cli}-packages" [] {{',
            '  let root = ($env.PWD | path expand)',
            '  let src_dir = ($root | path join "src")',
            '  if ($src_dir | path exists) {',
            '    ls $src_dir | where type == dir | get name | path basename | each { |pkg|',
            f'      {{ value: $pkg, description: $"Drift package \'($pkg)\'" }}',
            '    }',
            '  } else {',
            '    []',
            '  }',
            '}',
            "",
            self._render_static_completer(f"{cli}-shells", SHELLS),
            "",
            self._render_static_completer(f"{cli}-help-topics", HELP_TOPICS),
            "",
            self._render_static_completer(f"{cli}-install-methods", INSTALL_METHODS),
            "",
            self._render_static_completer(f"{cli}-lifecycle-hooks", LIFECYCLE_HOOKS),
            "",
            "# =============================================================================",
            "# Root CLI Command",
            "# =============================================================================",
            "",
            f"# {self.schema.description}",
            'export extern "main" [',
        ]

        # Add global options to root extern
        lines.append(f'  --help(-h)                                      # Display help information')
        for opt in self.schema.global_options:
            lines.append(f"  {self._render_option_signature(opt)}")
        lines.append("]")
        lines.append("")

        # Add subcommands
        lines.append("# =============================================================================")
        lines.append("# Subcommands")
        lines.append("# =============================================================================")
        lines.append("")

        for cmd_name, cmd_spec in self.schema.commands.items():
            lines.extend(self._render_command(cmd_spec))
            lines.append("")

        return "\n".join(lines) + "\n"

    def _render_static_completer(self, name: str, choices: List[Choice]) -> str:
        """Renders a static record list completer for Nushell."""
        items: List[str] = []
        for c in choices:
            val_esc = c.value.replace('"', '\\"')
            desc_esc = c.description.replace('"', '\\"')
            items.append(f'    {{ value: "{val_esc}", description: "{desc_esc}" }}')
        return f'def "nu-complete {name}" [] {{\n  [\n' + "\n".join(items) + "\n  ]\n}"

    def _render_option_signature(self, opt: OptionSpec) -> str:
        """Renders a single OptionSpec as a Nushell extern flag signature."""
        long_flag = next((f for f in opt.flags if f.startswith("--")), None)
        short_flag = next((f for f in opt.flags if f.startswith("-") and not f.startswith("--")), None)

        if not long_flag:
            flag_str = short_flag or opt.flags[0]
        else:
            flag_name = long_flag[2:]
            if short_flag:
                short_name = short_flag[1:]
                flag_str = f"--{flag_name}(-{short_name})"
            else:
                flag_str = f"--{flag_name}"

        # Determine type annotation if option takes value
        if opt.takes_value:
            if opt.is_directory or opt.is_file:
                type_anno = ": path"
            elif opt.choices:
                if opt.choices == SHELLS:
                    type_anno = f': string@"nu-complete {self.schema.cli_name}-shells"'
                elif opt.choices == INSTALL_METHODS:
                    type_anno = f': string@"nu-complete {self.schema.cli_name}-install-methods"'
                elif opt.choices == HELP_TOPICS:
                    type_anno = f': string@"nu-complete {self.schema.cli_name}-help-topics"'
                elif opt.choices == LIFECYCLE_HOOKS:
                    type_anno = f': string@"nu-complete {self.schema.cli_name}-lifecycle-hooks"'
                else:
                    type_anno = ": string"
            else:
                type_anno = ": string"
        else:
            type_anno = ""

        full_decl = f"{flag_str}{type_anno}"
        desc = opt.description.replace("\n", " ").strip()
        return f"{full_decl:<48} # {desc}"

    def _render_positional_signature(self, pos: PositionalSpec) -> str:
        """Renders a single PositionalSpec as a Nushell extern positional signature."""
        name = pos.name.replace("-", "_")

        # Determine completer / type
        if pos.source_type == SourceType.DYNAMIC_PACKAGES:
            type_anno = f'string@"nu-complete {self.schema.cli_name}-packages"'
        elif pos.source_type == SourceType.FIXED_CHOICES:
            if pos.choices == SHELLS:
                type_anno = f'string@"nu-complete {self.schema.cli_name}-shells"'
            elif pos.choices == HELP_TOPICS:
                type_anno = f'string@"nu-complete {self.schema.cli_name}-help-topics"'
            elif pos.choices == INSTALL_METHODS:
                type_anno = f'string@"nu-complete {self.schema.cli_name}-install-methods"'
            elif pos.choices == LIFECYCLE_HOOKS:
                type_anno = f'string@"nu-complete {self.schema.cli_name}-lifecycle-hooks"'
            else:
                type_anno = "string"
        elif pos.source_type in (SourceType.DIRECTORIES, SourceType.FILES):
            type_anno = "path"
        else:
            type_anno = "string"

        # Determine prefix and optionality
        if pos.nargs in ("*", "+"):
            arg_str = f"...{name}: {type_anno}"
        elif not pos.required or pos.nargs == "?":
            arg_str = f"{name}?: {type_anno}"
        else:
            arg_str = f"{name}: {type_anno}"

        desc = pos.description.replace("\n", " ").strip()
        return f"{arg_str:<48} # {desc}"

    def _render_command(self, cmd: CommandSpec) -> List[str]:
        """Renders a complete subcommand extern block for Nushell."""
        cli = self.schema.cli_name
        lines: List[str] = [
            f"# {cmd.description}",
            f'export extern "{cli} {cmd.name}" [',
            f'  --help(-h)                                      # Display help information',
        ]

        # Options for this command
        for opt in cmd.options:
            lines.append(f"  {self._render_option_signature(opt)}")

        # Movable global options
        for g_opt in self.schema.global_options:
            if is_movable_global_option(g_opt):
                lines.append(f"  {self._render_option_signature(g_opt)}")

        # Positional arguments
        for pos in cmd.positionals:
            lines.append(f"  {self._render_positional_signature(pos)}")

        lines.append("]")
        return lines
