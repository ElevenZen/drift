"""Drift CLI Shell Completion Subpackage.

Provides native tab-completion generators for Bash, Zsh, and Fish shells.
"""

from typing import Optional

from ..schema import CompletionSchema, build_completion_schema, SHELLS
from .bash import BashGenerator
from .zsh import ZshGenerator
from .fish import FishGenerator


def generate_completion_script(shell: str, schema: Optional[CompletionSchema] = None) -> str:
    """Generates the native tab-completion script for the specified shell.

    Args:
        shell: Target shell ('bash', 'zsh', or 'fish').
        schema: Optional CompletionSchema instance. If omitted, uses build_completion_schema().

    Returns:
        The compiled shell completion script string.

    Raises:
        ValueError: If an unsupported shell type is requested.
    """
    if schema is None:
        schema = build_completion_schema()

    shell_lower = shell.strip().lower()
    if shell_lower == "bash":
        return BashGenerator(schema).generate()
    elif shell_lower == "zsh":
        return ZshGenerator(schema).generate()
    elif shell_lower == "fish":
        return FishGenerator(schema).generate()
    else:
        valid = ", ".join([c.value for c in SHELLS])
        raise ValueError(f"Unsupported shell '{shell}'. Supported shells are: {valid}")


__all__ = [
    "BashGenerator",
    "ZshGenerator",
    "FishGenerator",
    "generate_completion_script",
]
