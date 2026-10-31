"""Functional control-flow, null-safety, and collection helpers for Drift.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Zero-dependency functional primitives used across all layers of Drift:

    partition(pred, items) — Splits an iterable into (matches, non_matches) in one pass.
    star_partition(pred, items) — Splits an iterable of tuples by unpacking each item into pred(*item).
    star_filter(pred, items) — Filters an iterable of tuples by unpacking each item into pred(*item).
    SpliceSpan(start, end, replacement) & splice_spans(seq, spans) — Single-pass O(N) gap-interleaving sequence splicer.
    branch(pred1, fn1, ..., default_fn) — Lazy odd-arity when-then-else conditional evaluation.
    choose(pred1, val1, ..., default_val) — Eager odd-arity when-then-else conditional selection.
    when_not_none(a, b) — Evaluates/returns b when a is not None, otherwise None.
    when_none(a, b) — Returns a when a is not None, otherwise evaluates/returns fallback b.

===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from itertools import chain, starmap
from typing import (
    Any,
    Callable,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
    overload,
)

S = TypeVar("S")
T = TypeVar("T")
Predicate = Union[bool, Callable[[], Any]]


def _eval_predicate(predicate: Predicate) -> bool:
    """Evaluates a boolean condition or zero-argument predicate callable."""
    return bool(predicate() if callable(predicate) else predicate)


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


def star_partition(
    pred: Callable[..., Any],
    items: Iterable[T],
) -> Tuple[List[T], List[T]]:
    """Partitions an iterable of tuples into `(matches, non_matches)` by unpacking each item into `pred(*item)`."""
    return partition(lambda item: bool(pred(*cast(Iterable[Any], item))), items)


def star_filter(
    pred: Callable[..., Any],
    items: Iterable[T],
) -> Iterator[T]:
    """Filters an iterable of tuples by unpacking each item into `pred(*item)` (counterpart of `itertools.starmap`)."""
    return filter(lambda item: bool(pred(*cast(Iterable[Any], item))), items)


@overload
def branch(
    predicate: Predicate,
    lambda1: Callable[[], T],
    lambda2: Callable[[], T],
    /,
) -> T: ...


@overload
def branch(
    pred1: Predicate,
    fn1: Callable[[], T],
    pred2: Predicate,
    fn2: Callable[[], T],
    default_fn: Callable[[], T],
    /,
) -> T: ...


@overload
def branch(
    pred1: Predicate,
    fn1: Callable[[], T],
    pred2: Predicate,
    fn2: Callable[[], T],
    pred3: Predicate,
    fn3: Callable[[], T],
    default_fn: Callable[[], T],
    /,
) -> T: ...


@overload
def branch(
    pred1: Predicate,
    fn1: Callable[[], T],
    pred2: Predicate,
    fn2: Callable[[], T],
    pred3: Predicate,
    fn3: Callable[[], T],
    pred4: Predicate,
    fn4: Callable[[], T],
    default_fn: Callable[[], T],
    /,
) -> T: ...


@overload
def branch(
    predicate: Predicate,
    lambda1: Callable[[], T],
    *rest: Any,
) -> T: ...


def branch(
    predicate: Predicate,
    lambda1: Callable[[], T],
    *rest: Any,
) -> T:
    """Lazy odd-arity `when-then-...-else` evaluator (`branch(p1, fn1, p2, fn2, ..., default_fn)`).

    Evaluates predicates from left to right and invokes only the zero-argument callable paired
    with the first truthy predicate; if all predicates are falsy, invokes the trailing fallback
    callable. Raises `ValueError` if given an even number of arguments.
    """
    if len(rest) == 1:
        return lambda1() if _eval_predicate(predicate) else cast(Callable[[], T], rest[0])()
    if len(rest) == 0 or len(rest) % 2 == 0:
        raise ValueError(
            "branch() requires an odd number of arguments: (pred1, fn1, ..., default_fn)"
        )
    if _eval_predicate(predicate):
        return lambda1()
    matched_fn = next(
        (
            cast(Callable[[], T], fn)
            for pred, fn in zip(rest[:-1:2], rest[1::2])
            if _eval_predicate(pred)
        ),
        cast(Callable[[], T], rest[-1]),
    )
    return matched_fn()


@overload
def choose(
    predicate: Predicate,
    value1: T,
    value2: T,
    /,
) -> T: ...


@overload
def choose(
    pred1: Predicate,
    val1: T,
    pred2: Predicate,
    val2: T,
    default: T,
    /,
) -> T: ...


@overload
def choose(
    pred1: Predicate,
    val1: T,
    pred2: Predicate,
    val2: T,
    pred3: Predicate,
    val3: T,
    default: T,
    /,
) -> T: ...


@overload
def choose(
    pred1: Predicate,
    val1: T,
    pred2: Predicate,
    val2: T,
    pred3: Predicate,
    val3: T,
    pred4: Predicate,
    val4: T,
    default: T,
    /,
) -> T: ...


@overload
def choose(
    predicate: Predicate,
    value1: T,
    *rest: Any,
) -> T: ...


def choose(
    predicate: Predicate,
    value1: T,
    *rest: Any,
) -> T:
    """Eager odd-arity `when-then-...-else` selector (`choose(p1, v1, p2, v2, ..., default)`).

    Evaluates predicates from left to right and returns the value paired with the first truthy
    predicate verbatim (callable values such as classes or functions are never invoked); if all
    predicates are falsy, returns the trailing default value. Raises `ValueError` if given an
    even number of arguments.

    Use `branch()` instead when candidate values are expensive, have side effects, or are only
    valid under their condition (e.g. file I/O, dict/index lookups, conversions that may raise).
    """
    if len(rest) == 1:
        return value1 if _eval_predicate(predicate) else cast(T, rest[0])
    if len(rest) == 0 or len(rest) % 2 == 0:
        raise ValueError(
            "choose() requires an odd number of arguments: (pred1, val1, ..., default)"
        )
    if _eval_predicate(predicate):
        return value1
    return next(
        (
            cast(T, val)
            for pred, val in zip(rest[:-1:2], rest[1::2])
            if _eval_predicate(pred)
        ),
        cast(T, rest[-1]),
    )


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


@dataclass(frozen=True)
class SpliceSpan(Generic[T]):
    """A slice replacement `seq[start:end] -> replacement` along a 1D sequence axis."""

    start: int
    end: int
    replacement: Tuple[T, ...]

    def overlaps_closed(self, other: SpliceSpan[Any]) -> bool:
        """True if closed intervals `[self.start, self.end]` and `[other.start, other.end]` touch or overlap."""
        return max(self.start, other.start) <= min(self.end, other.end)


def splice_spans(
    seq: Sequence[T],
    spans: Sequence[SpliceSpan[T]],
) -> Tuple[T, ...]:
    """Splices sorted, non-overlapping `SpliceSpan`s into `seq` in a single O(N) gap-interleaving pass."""
    if not spans:
        return tuple(seq)
    prev_ends = (0, *map(lambda s: s.end, spans))
    next_starts = (*map(lambda s: s.start, spans), len(seq))
    gaps = tuple(starmap(lambda start, end: seq[start:end], zip(prev_ends, next_starts)))
    interleaved = chain.from_iterable(
        starmap(lambda gap, span: (*gap, *span.replacement), zip(gaps[:-1], spans))
    )
    return (*interleaved, *gaps[-1])
