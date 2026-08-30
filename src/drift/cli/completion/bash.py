"""Bash Tab-Completion Script Generator for Drift CLI.

===============================================================================
How Bash Programmable Completion Works:
===============================================================================
Bash completion relies on the `complete` builtin command (typically backed by
the standard `bash-completion` package).

1. Registration:
   The `complete -F <function_name> <command_name>` directive tells Bash:
   "Whenever the user presses <TAB> while typing <command_name>, call <function_name>."

2. Standard Variables Provided by Bash:
   - `COMP_WORDS`: An array of words typed so far on the command line.
     Example: `COMP_WORDS=("drift" "-v" "deploy" "pk")`
   - `COMP_CWORD`: The integer index in `COMP_WORDS` of the word currently being completed.
     Example: `3` (pointing to `"pk"`).
   - `COMPREPLY`: The output array where the completion function stores matching suggestions.
     Bash reads `COMPREPLY` and displays the completion candidates.

3. Standard Helper (`_init_completion`):
   Provided by `/usr/share/bash-completion/bash_completion`, this helper extracts:
   - `$cur`: The current token under the cursor being completed (e.g. `"pk"` or `"--j"`).
   - `$prev`: The previous token immediately preceding `$cur` (e.g. `"deploy"` or `"-C"`).
   - `$words` and `$cword`: Normalized array and index.

4. Candidate Filtering with `compgen`:
   `compgen -W "deploy status diff" -- "$cur"` returns only the words that start with `$cur`.
   `compgen -f -- "$cur"` returns matching filesystem files.
   `compgen -d -- "$cur"` returns matching filesystem directories.

-------------------------------------------------------------------------------
Minimal Standalone Bash Completion Example:
-------------------------------------------------------------------------------
```bash
_sample_cli_completion() {
    local cur prev words cword
    _init_completion || return

    local subcommands="deploy status init diff"
    local global_flags="-v --verbose --json -C --directory"

    # If completing the first argument after CLI name, offer subcommands and global flags
    if [[ $cword -eq 1 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "$global_flags" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "$subcommands $global_flags" -- "$cur") )
        fi
        return 0
    fi
}
complete -F _sample_cli_completion sample_cli
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
    is_movable_global_option,
)


class BashGenerator:
    """Generates a native Bash programmable completion script for Drift CLI.

    Translates a declarative CompletionSchema into an optimized Bash completion
    script that executes in native shell memory without spawning a Python interpreter.
    """

    def __init__(self, schema: CompletionSchema) -> None:
        self.schema = schema

    def generate(self) -> str:
        """Compiles and returns the full Bash completion script as a string."""
        cli = self.schema.cli_name

        lines: List[str] = [
            f"# Bash completion script for {cli}",
            f"# Auto-generated from {cli}.cli.schema. DO NOT EDIT MANUALLY.",
            "",
            "# -----------------------------------------------------------------------------",
            "# Helper: Dynamic Workspace Package Discovery",
            "# -----------------------------------------------------------------------------",
            f"_{cli}_packages() {{",
            '    local drift_dir=""',
            '    local i=1',
            "    # Check if -C or --directory was specified earlier in the command line",
            '    while [[ $i -lt $COMP_CWORD ]]; do',
            '        if [[ "${COMP_WORDS[i]}" == "-C" || "${COMP_WORDS[i]}" == "--directory" ]]; then',
            '            drift_dir="${COMP_WORDS[i+1]}"',
            "            break",
            "        fi",
            "        ((i++))",
            "    done",
            "",
            '    local search_dir="${drift_dir:-.}"',
            '    if [[ -d "$search_dir/src" ]]; then',
            '        command ls -1 "$search_dir/src" 2>/dev/null',
            "    else",
            "        # Walk up directory tree looking for drift.toml or src/",
            '        local cur_path="$(pwd)"',
            '        while [[ "$cur_path" != "/" && "$cur_path" != "." ]]; do',
            '            if [[ -d "$cur_path/src" && ( -f "$cur_path/drift.toml" || -d "$cur_path/render" ) ]]; then',
            '                command ls -1 "$cur_path/src" 2>/dev/null',
            "                break",
            "            fi",
            '            cur_path="$(dirname "$cur_path")"',
            "        done",
            "    fi",
            "}",
            "",
            "# -----------------------------------------------------------------------------",
            f"# Main Programmable Completion Dispatcher for '{cli}'",
            "# -----------------------------------------------------------------------------",
            f"_{cli}_completion() {{",
            "    local cur prev words cword",
            "    _init_completion || return",
            "",
            f'    local subcommands="{" ".join(self.schema.commands.keys())}"',
            f'    local global_options="{" ".join(self._get_global_flags())}"',
            "",
            "    # 1. Determine active subcommand and count positional arguments",
            '    local cmd=""',
            "    local cmd_index=0",
            "    local pos_count=0",
            "    local i=1",
            "",
            '    while [[ $i -lt $cword ]]; do',
            '        local word="${words[i]}"',
            '        if [[ -z "$cmd" ]]; then',
            '            if [[ "$word" != -* ]]; then',
            "                # Check if this word is a recognized subcommand",
            '                case " $subcommands " in',
            '                    *" $word "*)',
            '                        cmd="$word"',
            "                        cmd_index=$i",
            "                        ;;",
            "                esac",
            "            elif [[ \"$word\" == \"-C\" || \"$word\" == \"--directory\" ]]; then",
            "                ((i++)) # Skip option value",
            "            fi",
            "        else",
            '            if [[ "$word" != -* ]]; then',
            "                ((pos_count++))",
            "            else",
            "                # Skip option values if option takes argument",
            f"                case \"$cmd:$word\" in",
        ]

        # Add cases for options taking values across commands
        for cmd_name, cmd in self.schema.commands.items():
            for opt in cmd.options:
                if opt.takes_value:
                    for flag in opt.flags:
                        lines.append(f'                    "{cmd_name}:{flag}") ((i++)) ;;')

        lines.extend([
            "                esac",
            "            fi",
            "        fi",
            "        ((i++))",
            "    done",
            "",
            "    # 2. Complete root level (global options & subcommands) if no subcommand set",
            '    if [[ -z "$cmd" ]]; then',
            '        if [[ "$cur" == -* ]]; then',
            '            COMPREPLY=( $(compgen -W "$global_options" -- "$cur") )',
            "            return 0",
            "        else",
            '            COMPREPLY=( $(compgen -W "$subcommands $global_options" -- "$cur") )',
            "            return 0",
            "        fi",
            "    fi",
            "",
            "    # 3. Handle option values if prev is an option flag expecting value",
            '    case "$cmd:$prev" in',
        ])

        # Generate option-specific value completions
        for cmd_name, cmd in self.schema.commands.items():
            for opt in cmd.options:
                if not opt.takes_value:
                    continue
                for flag in opt.flags:
                    case_pattern = f'        "{cmd_name}:{flag}")'
                    lines.append(case_pattern)
                    if opt.choices:
                        choice_vals = " ".join(c.value for c in opt.choices)
                        lines.append(f'            COMPREPLY=( $(compgen -W "{choice_vals}" -- "$cur") )')
                    elif opt.is_directory:
                        lines.append('            COMPREPLY=( $(compgen -d -- "$cur") )')
                    elif opt.is_file:
                        lines.append('            COMPREPLY=( $(compgen -f -- "$cur") )')
                    else:
                        lines.append("            return 0")
                    lines.append("            return 0")
                    lines.append("            ;;")

        # Global option flags taking value (e.g. -C / --directory)
        lines.extend([
            '        *:"-C"|*:"--directory")',
            '            COMPREPLY=( $(compgen -d -- "$cur") )',
            "            return 0",
            "            ;;",
            "    esac",
            "",
            "    # 4. Handle subcommand flags if current word begins with '-'",
            '    if [[ "$cur" == -* ]]; then',
            '        case "$cmd" in',
        ])

        for cmd_name, cmd in self.schema.commands.items():
            cmd_flags = self._get_command_flags(cmd)
            lines.append(f'            "{cmd_name}")')
            lines.append(f'                COMPREPLY=( $(compgen -W "{" ".join(cmd_flags)}" -- "$cur") )')
            lines.append("                return 0")
            lines.append("                ;;")

        lines.extend([
            "        esac",
            "    fi",
            "",
            "    # 5. Handle positional arguments based on pos_count",
            '    case "$cmd" in',
        ])

        for cmd_name, cmd in self.schema.commands.items():
            lines.append(f'        "{cmd_name}")')
            lines.append(self._generate_command_positionals(cmd))
            lines.append("            ;;")

        lines.extend([
            "    esac",
            "} &&",
            f"complete -F _{cli}_completion {cli}",
            "",
        ])

        return "\n".join(lines)

    def _get_global_flags(self) -> List[str]:
        flags: List[str] = []
        for opt in self.schema.global_options:
            flags.extend(opt.flags)
        return flags

    def _get_command_flags(self, cmd: CommandSpec) -> List[str]:
        flags: List[str] = []
        for opt in cmd.options:
            flags.extend(opt.flags)
        for g_opt in self.schema.global_options:
            if is_movable_global_option(g_opt):
                for f in g_opt.flags:
                    if f not in flags:
                        flags.append(f)
        return flags

    def _generate_command_positionals(self, cmd: CommandSpec) -> str:
        if not cmd.positionals:
            return '            return 0'

        lines: List[str] = []
        lines.append('            case "$pos_count" in')

        for idx, pos in enumerate(cmd.positionals):
            pattern = str(idx)
            lines.append(f'                {pattern})')
            lines.append(self._get_completion_for_source_type(pos))
            lines.append('                    return 0')
            lines.append('                    ;;')

        # If the last positional is repeatable, handle higher positional counts
        last_pos = cmd.positionals[-1]
        if last_pos.repeatable:
            lines.append('                *)')
            lines.append(self._get_completion_for_source_type(last_pos))
            lines.append('                    return 0')
            lines.append('                    ;;')
        else:
            lines.append('                *)')
            lines.append('                    return 0')
            lines.append('                    ;;')

        lines.append('            esac')
        return "\n".join(lines)

    def _get_completion_for_source_type(self, pos: PositionalSpec) -> str:
        cli = self.schema.cli_name
        if pos.source_type == SourceType.DYNAMIC_PACKAGES:
            return f'                    COMPREPLY=( $(compgen -W "$(__{cli}_packages)" -- "$cur") )'.replace(f'__{cli}', f'_{cli}')
        elif pos.source_type == SourceType.FIXED_CHOICES and pos.choices:
            vals = " ".join(c.value for c in pos.choices)
            return f'                    COMPREPLY=( $(compgen -W "{vals}" -- "$cur") )'
        elif pos.source_type == SourceType.FILES:
            return '                    COMPREPLY=( $(compgen -f -- "$cur") )'
        elif pos.source_type == SourceType.DIRECTORIES:
            return '                    COMPREPLY=( $(compgen -d -- "$cur") )'
        else:
            return '                    COMPREPLY=()'
