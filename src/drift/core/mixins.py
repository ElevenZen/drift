"""Reusable behavioral mixins and class decorators for Drift domain models.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 0: Core Behavioral Constraints
    - AlwaysTruthy: Mixin guaranteeing instances always evaluate to True in boolean contexts.
    - always_truthy: Class decorator applying the AlwaysTruthy boolean behavior.
===============================================================================
"""

from typing import TypeVar

T = TypeVar("T", bound=type)


class AlwaysTruthy:
    """Mixin enforcing that instances always evaluate to True in boolean contexts.

    Python's data model evaluates collections (classes implementing `__len__`) as False
    when empty. Inheriting from `AlwaysTruthy` guarantees that `__bool__` returns True,
    enabling safe `a or default` idiom usage without risk of empty collection truncation.
    """

    def __bool__(self) -> bool:
        return True


def always_truthy(cls: T) -> T:
    """Class decorator guaranteeing instances always evaluate to True in boolean contexts."""
    cls.__bool__ = lambda self: True
    return cls
