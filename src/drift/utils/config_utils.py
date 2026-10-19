"""Configuration dictionary and value manipulation utilities for Drift.

Provides helpers for safely accessing, traversing, setting, validating, and coercing
configuration mappings and values across Drift packages and workspace configs.
"""

import inspect
from typing import Any, Callable, Iterable, List, Optional, Mapping, Sequence, Tuple, TypeVar, Union, cast, overload

from ..core.exceptions import ConfigError

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



def get_first_from(
    data: Optional[Mapping[str, Any]],
    keys: Iterable[str],
    default: Any = None,
) -> Any:
    """Retrieves the first present value from a dictionary using an iterable of alternative key names.

    Args:
        data: Dictionary or mapping to search for keys.
        keys: Candidate keys in order of precedence (can be a generator, tuple, list, etc.).
        default: Fallback value if none of the candidate keys are present in data.

    Returns:
        The value of the first matching key in data, or default if no keys match.
    """
    if not isinstance(data, (dict, Mapping)):
        return default
    return next((data[key] for key in keys if key in data), default)


def get_nested_from(
    data: Optional[Mapping[str, Any]],
    keys: Union[str, Sequence[str]],
    default: Any = None,
    required: bool = False,
    is_table: bool = False,
    context: str = "configuration",
) -> Any:
    """Retrieves a nested value from a mapping given a dot-delimited key path or key sequence.

    Args:
        data: Mapping to traverse.
        keys: Dot-separated path string (e.g. "packages.enable") or sequence of keys.
        default: Fallback value if the nested path is missing and required is False.
        required: If True, raises ConfigError when the path does not exist.
        is_table: If True and the retrieved value is not a table/mapping, raises ConfigError.
        context: Context descriptor (e.g. "workspace configuration", "package configuration")
            used in error messages.

    Returns:
        The nested value at the specified key path, or `default` if not found.

    Raises:
        ConfigError: If required is True and the key path is missing, or if is_table is True
            and the found value is not a mapping.
    """
    path_str = keys if isinstance(keys, str) else ".".join(str(k) for k in keys)
    path_parts = [k.strip() for k in keys.split(".")] if isinstance(keys, str) else [str(k) for k in keys]

    if not isinstance(data, (dict, Mapping)):
        if required:
            raise ConfigError(f"Missing '[{path_str}]' section in {context}.")
        return default

    val: Any = data
    for key in path_parts:
        if not isinstance(val, (dict, Mapping)) or key not in val:
            if required:
                raise ConfigError(f"Missing '[{path_str}]' section in {context}.")
            return default
        val = val[key]

    if val is None:
        if required:
            raise ConfigError(f"Missing '[{path_str}]' section in {context}.")
        return default

    if is_table and not isinstance(val, (dict, Mapping)):
        raise ConfigError(f"'[{path_str}]' must be a TOML table.")

    return val


def parse_bool_value(
    val: Any,
    default: bool = False,
    strict: bool = False,
    context: str = "",
) -> bool:
    """Coerces a boolean, string, or numeric value into a boolean.

    Recognizes standard boolean truthy strings: 'true', '1', 'yes', 'on', 'enable', 'enabled' (case-insensitive).
    Recognizes standard boolean falsy strings: 'false', '0', 'no', 'off', 'disable', 'disabled' (case-insensitive).

    Args:
        val: Value to parse or coerce.
        default: Fallback boolean value if val is None.
        strict: If True, raises ConfigError for unparseable strings or non-boolean types.
        context: Optional description of the field or section for error messages when strict=True.

    Returns:
        The coerced boolean value.

    Raises:
        ConfigError: If strict is True and val cannot be parsed as a valid boolean.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if strict and val not in (0, 1):
            ctx_str = f" under {context}" if context else ""
            raise ConfigError(f"Invalid boolean value '{val}'{ctx_str} (expected 0 or 1).")
        return bool(val)
    if isinstance(val, str):
        cleaned = val.strip().lower()
        if cleaned in ("true", "1", "yes", "on", "enable", "enabled"):
            return True
        if cleaned in ("false", "0", "no", "off", "disable", "disabled"):
            return False
        if strict:
            ctx_str = f" under {context}" if context else ""
            raise ConfigError(f"Invalid boolean value '{val}'{ctx_str}.")
        return default
    if strict:
        ctx_str = f" under {context}" if context else ""
        raise ConfigError(f"Expected boolean value, got {type(val).__name__}{ctx_str}.")
    return bool(val)


def validate_known_keys(
    data: Optional[Mapping[str, Any]],
    known_keys: Iterable[str],
    context: str = "",
    message_prefix: Optional[str] = None,
    suffix: str = "",
) -> None:
    """Validates that all keys in a mapping are within a set of known valid keys.

    Uses a functional filter to gather all unknown keys and raises a ConfigError
    reporting all unknown keys if any are found.

    Args:
        data: The dictionary or mapping to validate.
        known_keys: Iterable of allowed/known key names.
        context: Context descriptor (e.g. "[settings]", "requirements") used to build
            the error message prefix if message_prefix is not explicitly provided.
        message_prefix: Explicit prefix for the error message (e.g. "Unknown workspace option").
        suffix: Suffix appended to the error message (e.g. " for package 'foo'").

    Raises:
        ConfigError: If any keys in data are not in known_keys.
    """
    if not data or not isinstance(data, (dict, Mapping)):
        return

    known_set = set(known_keys)
    unknown_keys = list(filter(lambda k: k not in known_set, data.keys()))
    if not unknown_keys:
        return

    keys_str = ", ".join(f"'{k}'" for k in unknown_keys)
    if message_prefix is not None:
        prefix = message_prefix
    elif context:
        prefix = f"Unknown option under {context}"
    else:
        prefix = "Unknown option"

    raise ConfigError(f"{prefix}: {keys_str}{suffix}")


def set_nested_val(data: dict, keys: list, value: Any) -> None:
    """Sets a value in a nested dictionary given a list of keys."""
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
