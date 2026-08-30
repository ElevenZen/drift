"""Fish Tab-Completion Script Generator for Drift CLI.

===============================================================================
How Fish Shell Completion Works:
===============================================================================
Fish has a clean, fully declarative built-in completion system based on the
`complete` command. Unlike Bash and Zsh which use imperative control flows,
Fish completions are defined as independent declarative rules.

1. The `complete` Builtin:
   - `-c <command_name>`: Specifies which command this rule belongs to.
   - `-f`: Disables default filename completions when typing options/subcommands.
   - `-F`: Re-enables filename completions where explicit file paths are expected.
   - `-s <short_flag>` / `-l <long_flag>`: Associates short and long flags.
   - `-d "<description>"`: Displays documentation in interactive menus.
   - `-a "<candidates>"`: Supplies matching argument values.
   - `-r`: Marks an option as requiring an argument.

2. Condition Predicates (`-n "<condition>"`):
   Fish evaluates the condition string before offering the completion rule:
   - `__fish_use_subcommand`: True when no subcommand has been typed yet.
     Used for root-level global options and the list of subcommands.
   - `__fish_seen_subcommand_from <subcmd>`: True when `<subcmd>` has already
     been typed on the command line.
   - Positional Argument Indexing:
     `test (count (commandline -poc)) -eq N`
     - `commandline -poc` outputs the tokenized command line excluding options.
     - Testing its count determines the exact positional index (e.g. 1st, 2nd arg).

3. Rich Interactive Menus:
   Fish automatically displays candidate values with descriptions formatted
   neatly in an interactive multi-column pager.

-------------------------------------------------------------------------------
Minimal Standalone Fish Completion Example:
-------------------------------------------------------------------------------
```fish
# Disable default file completions for sample_cli
complete -c sample_cli -f

# 1. Global options at root level
complete -c sample_cli -n "__fish_use_subcommand" -s v -l verbose -d "Enable verbose output"
complete -c sample_cli -n "__fish_use_subcommand" -l json -d "Output results in JSON format"

# 2. Subcommands with descriptions
complete -c sample_cli -n "__fish_use_subcommand" -a "deploy" -d "Deploy configuration templates"
complete -c sample_cli -n "__fish_use_subcommand" -a "status" -d "Audit active package status"

# 3. Subcommand options
complete -c sample_cli -n "__fish_seen_subcommand_from deploy" -s f -l force -d "Force deployment"

# 4. Positional arguments for deploy (target package names from dynamic function)
complete -c sample_cli -n "__fish_seen_subcommand_from deploy; and test (count (commandline -poc)) -ge 2" \\
    -a "(__sample_cli_packages)" -d "Target package"
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
)


class FishGenerator:
    """Generates a native Fish shell completion script for Drift CLI.

    Translates a declarative CompletionSchema into modular `complete` rules
    with rich inline documentation descriptions and dynamic package evaluation.
    """

    def __init__(self, schema: CompletionSchema) -> None:
        self.schema = schema

    def generate(self) -> str:
        """Compiles and returns the full Fish completion script as a string."""
        cli = self.schema.cli_name

        lines: List[str] = [
            f"# Fish completion script for {cli}",
            f"# Auto-generated from {cli}.cli.schema. DO NOT EDIT MANUALLY.",
            f"complete -c {cli} -f",
            "",
            "# -----------------------------------------------------------------------------",
            "# Helper: Dynamic Workspace Package Discovery",
            "# -----------------------------------------------------------------------------",
            f"function __{cli}_packages",
            "    set -l root (pwd)",
            "    set -l cmd_tokens (commandline -poc)",
            "    for i in (seq (count $cmd_tokens))",
            '        if test "$cmd_tokens[$i]" = "-C" -o "$cmd_tokens[$i]" = "--directory"',
            "            set -l next_idx (math $i + 1)",
            "            if test $next_idx -le (count $cmd_tokens)",
            "                set root $cmd_tokens[$next_idx]",
            "                break",
            "            end",
            "        end",
            "    end",
            "",
            '    if test -d "$root/src"',
            '        command ls -1 "$root/src" 2>/dev/null',
            "    else",
            '        while test "$root" != "/" -a "$root" != "."',
            '            if test -d "$root/src" -a \\( -f "$root/drift.toml" -o -d "$root/render" \\)',
            '                command ls -1 "$root/src" 2>/dev/null',
            "                break",
            "            end",
            '            set root (dirname "$root")',
            "        end",
            "    end",
            "end",
            "",
            "# -----------------------------------------------------------------------------",
            "# 1. Global options at root level",
            "# -----------------------------------------------------------------------------",
        ]

        # 1. Global options on root
        for opt in self.schema.global_options:
            lines.extend(self._format_fish_option(opt, condition="__fish_use_subcommand"))

        lines.extend([
            "",
            "# -----------------------------------------------------------------------------",
            "# 2. Subcommands with interactive menu descriptions",
            "# -----------------------------------------------------------------------------",
        ])

        # 2. Subcommand declarations
        for cmd in self.schema.commands.values():
            desc = self._escape_fish_desc(cmd.description)
            lines.append(
                f'complete -c {cli} -n "__fish_use_subcommand" '
                f'-a "{cmd.name}" -d "{desc}"'
            )

        lines.extend([
            "",
            "# -----------------------------------------------------------------------------",
            "# 3. Subcommand options and positional arguments",
            "# -----------------------------------------------------------------------------",
        ])

        # 3. Subcommand options and positionals
        for cmd_name, cmd in self.schema.commands.items():
            cond = f"__fish_seen_subcommand_from {cmd_name}"

            # Command options
            for opt in cmd.options:
                lines.extend(self._format_fish_option(opt, condition=cond))

            # Movable global options (e.g. --json, -v/--verbose)
            existing_flags = {f for o in cmd.options for f in o.flags}
            for g_opt in self.schema.global_options:
                if is_movable_global_option(g_opt) and not any(f in existing_flags for f in g_opt.flags):
                    lines.extend(self._format_fish_option(g_opt, condition=cond))

            # Positional arguments
            for idx, pos in enumerate(cmd.positionals, start=1):
                pos_cond = f"{cond}; and test (count (commandline -poc)) -eq {idx + 1}"
                if pos.repeatable and idx == len(cmd.positionals):
                    pos_cond = f"{cond}; and test (count (commandline -poc)) -ge {idx + 1}"

                lines.extend(self._format_fish_positional(pos, pos_cond))

        lines.append("")
        return "\n".join(lines)

    def _format_fish_option(self, opt: OptionSpec, condition: Optional[str] = None) -> List[str]:
        cli = self.schema.cli_name
        desc = self._escape_fish_desc(opt.description)

        short_flag = None
        long_flag = None
        for f in opt.flags:
            if f.startswith("--") and not long_flag:
                long_flag = f.lstrip("-")
            elif f.startswith("-") and not f.startswith("--") and not short_flag:
                short_flag = f.lstrip("-")

        parts = [f"complete -c {cli}"]
        if condition:
            parts.append(f'-n "{condition}"')
        if short_flag:
            parts.append(f"-s {short_flag}")
        if long_flag:
            parts.append(f"-l {long_flag}")
        if desc:
            parts.append(f'-d "{desc}"')

        if opt.takes_value:
            parts.append("-r")
            if opt.choices:
                # Add choices with descriptions
                lines = []
                for c in opt.choices:
                    c_desc = self._escape_fish_desc(c.description)
                    c_parts = list(parts)
                    c_parts.append(f'-a "{c.value}"')
                    if c_desc:
                        c_parts.append(f'-d "{c_desc}"')
                    lines.append(" ".join(c_parts))
                return lines
            elif opt.is_directory:
                parts.append('-a "(__fish_complete_directories)"')
            elif opt.is_file:
                parts.append("-F")

        return [" ".join(parts)]

    def _format_fish_positional(self, pos: PositionalSpec, condition: str) -> List[str]:
        cli = self.schema.cli_name
        desc = self._escape_fish_desc(pos.description)
        lines: List[str] = []

        if pos.source_type == SourceType.DYNAMIC_PACKAGES:
            lines.append(
                f'complete -c {cli} -n "{condition}" '
                f'-a "(__{cli}_packages)" -d "{desc}"'
            )
        elif pos.source_type == SourceType.FIXED_CHOICES and pos.choices:
            for c in pos.choices:
                c_desc = self._escape_fish_desc(c.description)
                lines.append(
                    f'complete -c {cli} -n "{condition}" '
                    f'-a "{c.value}" -d "{c_desc}"'
                )
        elif pos.source_type == SourceType.FILES:
            lines.append(f'complete -c {cli} -n "{condition}" -F')
        elif pos.source_type == SourceType.DIRECTORIES:
            lines.append(
                f'complete -c {cli} -n "{condition}" '
                f'-r -a "(__fish_complete_directories)"'
            )

        return lines

    def _escape_fish_desc(self, text: str) -> str:
        """Escapes quotes in Fish descriptions."""
        return text.replace('"', '\\"').replace("'", "\\'")
