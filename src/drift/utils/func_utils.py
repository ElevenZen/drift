"""Functional control-flow, null-safety, and collection helpers for Drift.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Zero-dependency functional primitives used across all layers of Drift:

    partition(pred, items) — Splits an iterable into (matches, non_matches) in one pass.
    branch(predicate, lambda1, lambda2) — Lazy conditional evaluation (invokes lambda1() or lambda2()).
    choose(predicate, value1, value2) — Eager conditional selection (returns value1 or value2 verbatim).
    when_not_none(a, b) — Evaluates/returns b when a is not None, otherwise None.
    when_none(a, b) — Returns a when a is not None, otherwise evaluates/returns fallback b.

===============================================================================
"""

import inspect
from typing import Any, Callable, Iterable, List, Optional, Tuple, TypeVar, Union, cast, overload

S = TypeVar("S")
T = TypeVar("T")


def partition(
    pred: Callable[[T], bool],
    items: Iterable[T],
) -> Tuple[List[T], List[T]]:
    """Partitions an iterable into two lists based on a predicate: (matches, non_matches).

    Args:
        pred: Predicate function evaluated on each element.
        items: Sequence or iterable of elements to partition.

    Returns:
        A tuple of (trues, falses) where trues contains items for which pred(x) is True,
        and falses contains items for which pred(x) is False.
    """
    trues: List[T] = []
    falses: List[T] = []
    for item in items:
        (trues if pred(item) else falses).append(item)
    return trues, falses


def branch(
    predicate: Union[bool, Callable[[], Any]],
    lambda1: Callable[[], T],
    lambda2: Callable[[], T],
) -> T:
    """Evaluates predicate and invokes lambda1() if truthy, otherwise lambda2().

    Args:
        predicate: A boolean condition or zero-argument callable returning a truthy/falsy value.
        lambda1: Zero-argument callable invoked and returned when predicate is truthy.
        lambda2: Zero-argument callable invoked and returned when predicate is falsy.

    Returns:
        The result of lambda1() if predicate is truthy, otherwise lambda2().
    """
    cond = predicate() if callable(predicate) else bool(predicate)
    return lambda1() if cond else lambda2()


def choose(
    predicate: Union[bool, Callable[[], Any]],
    value1: T,
    value2: T,
) -> T:
    """Eager counterpart of `branch()`: returns value1 if predicate is truthy, otherwise value2.

    Both values are evaluated by the caller before the call and are returned verbatim
    (callables such as classes or functions are never invoked). Use `branch()` instead
    when either side is expensive, has side effects, or is only valid under its condition
    (e.g. file I/O, dict lookups, type conversions that may raise).

    Args:
        predicate: A boolean condition or zero-argument callable returning a truthy/falsy value.
        value1: Value returned when predicate is truthy.
        value2: Value returned when predicate is falsy.

    Returns:
        value1 if predicate is truthy, otherwise value2.
    """
    cond = predicate() if callable(predicate) else bool(predicate)
    return value1 if cond else value2


@overload
def when_not_none(a: None, b: Any) -> None: ...


@overload
def when_not_none(a: Optional[S], b: Callable[[S], Optional[T]]) -> Optional[T]: ...


@overload
def when_not_none(a: Any, b: Callable[[], Optional[T]]) -> Optional[T]: ...


@overload
def when_not_none(a: Any, b: Optional[T]) -> Optional[T]: ...


def when_not_none(
    a: Optional[S],
    b: Union[Callable[[S], Optional[T]], Callable[[], Optional[T]], Optional[T]],
) -> Optional[T]:
    """Chooses `b` when `a` is not None, otherwise returns None.

    Provides a strongly-typed replacement for `(a and b)` when selecting or deriving `b`
    conditioned on `a is not None`, without leaking the type of `a` into the return union.

    Args:
        a: Optional value to check against None.
        b: A 1-argument callable `fn(a)`, a 0-argument callable `fn()`,
            or a direct value to return when `a is not None`.

    Returns:
        The result of invoking `b(a)` / `b()` (if callable) or `b` directly
        when `a is not None`; otherwise `None`.
    """
    if a is None:
        return None
    if callable(b):
        try:
            sig = inspect.signature(b)
            pos_params = [
                p
                for p in sig.parameters.values()
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
            return branch(
                len(pos_params) == 0 and not has_varargs,
                lambda: cast(Callable[[], Optional[T]], b)(),
                lambda: cast(Callable[[S], Optional[T]], b)(a),
            )
        except (ValueError, TypeError):
            return cast(Callable[[S], Optional[T]], b)(a)
    return b


@overload
def when_none(a: None, b: Callable[[], T]) -> T: ...


@overload
def when_none(a: None, b: T) -> T: ...


@overload
def when_none(a: Optional[S], b: Callable[[], T]) -> Union[S, T]: ...


@overload
def when_none(a: Optional[S], b: T) -> Union[S, T]: ...


def when_none(
    a: Optional[S],
    b: Union[Callable[[], T], T],
) -> Union[S, T]:
    """Chooses fallback `b` when `a` is None, otherwise returns `a`.

    Provides a strongly-typed null-coalescing helper (`a if a is not None else b`)
    supporting both eager values and lazy 0-argument callables.

    Args:
        a: Optional primary value to check against None.
        b: A 0-argument callable `fn()` or direct fallback value used when `a is None`.

    Returns:
        `a` when `a is not None`; otherwise `b()` (if callable) or `b`.
    """
    if a is not None:
        return a
    return branch(callable(b), lambda: cast(Callable[[], T], b)(), lambda: cast(T, b))
