"""Zsh Tab-Completion Script Generator for Drift CLI.

===============================================================================
How Zsh Programmable Completion (Compsys) Works:
===============================================================================
Zsh has one of the most powerful and expressive completion systems in modern
Unix environments (`compsys`), enabled via `autoload -Uz compinit && compinit`.

1. File Header Tag (`#compdef <command_name>`):
   When a completion script begins with `#compdef drift`, Zsh automatically
   registers this file in its `$fpath` to handle tab-completions for `drift`.

2. The `_arguments` Utility Engine:
   `_arguments` parses the command line, evaluates options, checks mutual exclusions,
   and handles subcommands and positional arguments declaratively:
   - Option Specifications:
     `'(-f --force)'{-f,--force}'[Force re-initialization]'`
     - `(-f --force)` tells Zsh that if `-f` is used, `--force` cannot be repeated (mutual exclusion).
     - `{-f,--force}` binds both the short and long flag names.
     - `[Description text]` displays documentation in interactive menus.
   - Value Options:
     `'(-m --method)'{-m,--method}'[Installation method]:method:__drift_install_methods'`
     - `:method:` names the argument.
     - `__drift_install_methods` is a helper function called to provide candidate choices.
   - State Transition Pattern:
     `'1: :->command' '*:: :->args'`
     Directs the 1st positional word to transition `$state` to `command`, and any
     subsequent words to transition `$state` to `args` with `$line[1]` set to the subcommand.

3. Rich Interactive Menus with `_describe`:
   `_describe -t <tag> '<group_label>' <array_variable>`
   When array elements are formatted as `'value:Description text'`, Zsh renders
   a multi-column interactive selection menu with descriptions neatly aligned
   to the right of each candidate.

-------------------------------------------------------------------------------
Minimal Standalone Zsh Completion Example:
-------------------------------------------------------------------------------
```zsh
#compdef sample_cli

_sample_cli() {
    local curcontext="$curcontext" state line
    typeset -A opt_args

    # Declare root arguments and transition state
    _arguments -C \
        '(-v --verbose)'{-v,--verbose}'[Enable verbose output]' \
        '1: :->command' \
        '*:: :->args'

    case $state in
        command)
            local -a subcommands
            subcommands=(
                'deploy:Deploy declarative configuration templates'
                'status:Audit active packages across environments'
                'init:Initialize a new workspace'
            )
            _describe -t subcommands 'subcommand' subcommands
            ;;
        args)
            case $line[1] in
                deploy)
                    _arguments \
                        '(-f --force)'{-f,--force}'[Force deployment]' \
                        '*:package:_drift_packages'
                    ;;
            esac
            ;;
    esac
}

_sample_cli "$@"
```
===============================================================================
"""

from typing import List, Dict, Set
from ..schema import (
    CompletionSchema,
    CommandSpec,
    OptionSpec,
    PositionalSpec,
    SourceType,
    Choice,
    is_movable_global_option,
)


class ZshGenerator:
    """Generates a native Zsh completion function script for Drift CLI.

    Translates a declarative CompletionSchema into an interactive Zsh compsys
    definition with rich inline documentation menus and dynamic package discovery.
    """

    def __init__(self, schema: CompletionSchema) -> None:
        self.schema = schema

    def generate(self) -> str:
        """Compiles and returns the full Zsh completion script as a string."""
        cli = self.schema.cli_name

        lines: List[str] = [
            f"#compdef {cli}",
            f"# Zsh completion script for {cli}",
            f"# Auto-generated from {cli}.cli.schema. DO NOT EDIT MANUALLY.",
            "",
            "# -----------------------------------------------------------------------------",
            "# Helper: Dynamic Workspace Package Discovery",
            "# -----------------------------------------------------------------------------",
            f"_{cli}_packages() {{",
            "    local -a pkgs",
            '    local search_dir="."',
            "",
            "    # Check if -C or --directory was supplied in options",
            '    if (( ${+opt_args[-C]} )); then',
            '        search_dir="${opt_args[-C]}"',
            '    elif (( ${+opt_args[--directory]} )); then',
            '        search_dir="${opt_args[--directory]}"',
            "    fi",
            "",
            '    if [[ -d "$search_dir/src" ]]; then',
            '        pkgs=(${${(f)"$(ls -1 "$search_dir/src" 2>/dev/null)"}})',
            "    else",
            '        local cur_path="$(pwd)"',
            '        while [[ "$cur_path" != "/" && "$cur_path" != "." ]]; do',
            '            if [[ -d "$cur_path/src" && ( -f "$cur_path/drift.toml" || -d "$cur_path/render" ) ]]; then',
            '                pkgs=(${${(f)"$(ls -1 "$cur_path/src" 2>/dev/null)"}})',
            "                break",
            "            fi",
            '            cur_path="$(dirname "$cur_path")"',
            "        done",
            "    fi",
            "",
            "    if (( ${#pkgs} )); then",
            "        _describe -t packages 'package' pkgs",
            "    fi",
            "}",
            "",
        ]

        # Emit choice registries
        lines.extend(self._emit_choice_registries())

        # Main command entry point
        lines.extend([
            "# -----------------------------------------------------------------------------",
            f"# Main Entry Point for '{cli}'",
            "# -----------------------------------------------------------------------------",
            f"_{cli}() {{",
            '    local curcontext="$curcontext" state line',
            "    typeset -A opt_args",
            "",
            "    _arguments -C -s \\",
        ])

        # Global options
        for opt in self.schema.global_options:
            lines.append(f"        {self._format_zsh_option(opt)} \\")

        lines.extend([
            "        '1: :->command' \\",
            "        '*:: :->args'",
            "",
            "    case $state in",
            "        command)",
            "            local -a subcommands",
            "            subcommands=(",
        ])

        for cmd in self.schema.commands.values():
            desc = self._escape_zsh_desc(cmd.description)
            lines.append(f"                '{cmd.name}:{desc}'")

        lines.extend([
            "            )",
            "            _describe -t subcommands 'subcommand' subcommands",
            "            ;;",
            "        args)",
            "            case $line[1] in",
        ])

        for cmd_name, cmd in self.schema.commands.items():
            lines.append(f"                {cmd_name})")
            lines.append(f"                    _arguments -s \\")

            # Command options
            for opt in cmd.options:
                lines.append(f"                        {self._format_zsh_option(opt)} \\")

            # Movable global options (e.g. --json, -v/--verbose)
            existing_flags = {f for o in cmd.options for f in o.flags}
            for g_opt in self.schema.global_options:
                if is_movable_global_option(g_opt) and not any(f in existing_flags for f in g_opt.flags):
                    lines.append(f"                        {self._format_zsh_option(g_opt)} \\")

            # Positional arguments
            for idx, pos in enumerate(cmd.positionals, start=1):
                pos_spec = self._format_zsh_positional(idx, pos)
                lines.append(f"                        {pos_spec} \\")

            lines.append("                        && return 0")
            lines.append("                    ;;")

        lines.extend([
            "            esac",
            "            ;;",
            "    esac",
            "}",
            "",
            f"_{cli} \"$@\"",
            "",
        ])

        return "\n".join(lines)

    def _emit_choice_registries(self) -> List[str]:
        lines: List[str] = []
        cli = self.schema.cli_name

        # Gather all distinct choice sets
        choice_sets: Dict[str, List[Choice]] = {}
        for cmd in self.schema.commands.values():
            for pos in cmd.positionals:
                if pos.source_type == SourceType.FIXED_CHOICES and pos.choices:
                    key = pos.name
                    choice_sets[key] = pos.choices
            for opt in cmd.options:
                if opt.choices:
                    key = opt.dest or opt.flags[0].lstrip("-").replace("-", "_")
                    choice_sets[key] = opt.choices

        lines.append("# -----------------------------------------------------------------------------")
        lines.append("# Choice Registries with Interactive Menu Descriptions")
        lines.append("# -----------------------------------------------------------------------------")
        for name, choices in sorted(choice_sets.items()):
            func_name = f"_{cli}_{name}_choices"
            lines.append(f"{func_name}() {{")
            lines.append(f"    local -a items")
            lines.append(f"    items=(")
            for c in choices:
                desc = self._escape_zsh_desc(c.description)
                lines.append(f"        '{c.value}:{desc}'")
            lines.append(f"    )")
            lines.append(f"    _describe -t {name} '{name}' items")
            lines.append("}")
            lines.append("")

        return lines

    def _format_zsh_option(self, opt: OptionSpec) -> str:
        cli = self.schema.cli_name
        desc = self._escape_zsh_desc(opt.description)

        # Mutex exclusion prefix if multiple flags (e.g. '(-f --force)')
        mutex = ""
        if len(opt.flags) > 1:
            mutex = f"({' '.join(opt.flags)})"

        flag_str = opt.flags[0] if len(opt.flags) == 1 else "{" + ",".join(opt.flags) + "}"

        if not opt.takes_value:
            return f"'{mutex}{flag_str}[{desc}]'"

        action = ""
        arg_name = opt.dest or "value"
        if opt.choices:
            action = f":{arg_name}:_{cli}_{arg_name}_choices"
        elif opt.is_directory:
            action = f":directory:_files -/"
        elif opt.is_file:
            action = f":file:_files"
        else:
            action = f":{arg_name}:"

        return f"'{mutex}{flag_str}[{desc}]{action}'"

    def _format_zsh_positional(self, index: int, pos: PositionalSpec) -> str:
        cli = self.schema.cli_name
        desc = self._escape_zsh_desc(pos.description)
        prefix = f"{index}" if not pos.repeatable else "*"

        action = ""
        if pos.source_type == SourceType.DYNAMIC_PACKAGES:
            action = f"_{cli}_packages"
        elif pos.source_type == SourceType.FIXED_CHOICES and pos.choices:
            action = f"_{cli}_{pos.name}_choices"
        elif pos.source_type == SourceType.FILES:
            action = "_files"
        elif pos.source_type == SourceType.DIRECTORIES:
            action = "_files -/"
        else:
            action = " "

        return f"'{prefix}:{desc}:{action}'"

    def _escape_zsh_desc(self, text: str) -> str:
        """Escapes characters that have special meaning inside Zsh completion descriptions."""
        return text.replace(":", "\\:").replace("[", "\\[").replace("]", "\\]").replace("'", "'\\''")
