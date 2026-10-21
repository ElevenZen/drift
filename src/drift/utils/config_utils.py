"""Configuration dictionary and value manipulation utilities for Drift.

Provides helpers for safely accessing, traversing, setting, validating, and coercing
configuration mappings and values across Drift packages and workspace configs.
"""

import shlex
from pathlib import Path
from typing import Any, Iterable, List, Optional, Mapping, Sequence, Union, cast

from ..core.exceptions import ConfigError
from .func_utils import (
    branch,
    when_not_none,
)
from .path_utils import (
    check_is_partially_anchored_path,
    expand_path,
    is_relative_to,
)


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


def parse_command_tokens(
    raw_cmd: Any,
    field_name: str = "command",
    context: str = "",
) -> List[str]:
    """Parses a raw command configuration value (string or sequence of strings) into a List[str].

    Args:
        raw_cmd: Raw command value (None, str, list, or tuple).
        field_name: Name of the configuration field (e.g. "render_command", "decrypt_command").
        context: Optional context suffix (e.g. "for render engine 'jinja'", "under [secrets]").

    Returns:
        Tokenized list of non-empty strings (or [] if raw_cmd is None or blank).

    Raises:
        ConfigError: If raw_cmd has invalid shell syntax, non-string/empty list tokens, or an invalid type.
    """
    ctx = branch(bool(context), lambda: f" {context.strip()}", lambda: "")
    if raw_cmd is None:
        return []
    if isinstance(raw_cmd, str):
        stripped = raw_cmd.strip()
        if not stripped:
            return []
        try:
            return shlex.split(stripped, posix=True)
        except ValueError as exc:
            raise ConfigError(f"Invalid {field_name} syntax{ctx}: {exc}") from exc
    if isinstance(raw_cmd, (list, tuple)):
        if not all(isinstance(token, str) and bool(token.strip()) for token in raw_cmd):
            raise ConfigError(f"{field_name} list{ctx} must contain only non-empty strings.")
        return list(raw_cmd)
    raise ConfigError(
        f"{field_name}{ctx} must be a string or list of strings, got {type(raw_cmd).__name__}."
    )


def parse_config_path(
    raw_val: Any,
    field_name: str = "path",
    default: Optional[Path] = None,
    expand: bool = False,
    context: str = "",
) -> Path:
    """Coerces a raw configuration value (str or Path) into a Path, using default when None.

    Args:
        raw_val: Raw path value (None, str, or Path).
        field_name: Name of the configuration field for error reporting.
        default: Optional fallback Path when raw_val is None.
        expand: If True, expands '~' and environment variables via expand_path().
        context: Optional context suffix (e.g. "for package 'zsh'", "under [workspace]").

    Returns:
        Coerced (and optionally expanded) Path object.

    Raises:
        ConfigError: If raw_val is None without a default, or not a str or Path.
    """
    ctx = branch(bool(context), lambda: f" {context.strip()}", lambda: "")
    if raw_val is None:
        if default is None:
            raise ConfigError(f"{field_name}{ctx} must be a Path or str, got NoneType.")
        return branch(expand, lambda: expand_path(default), lambda: default)
    if not isinstance(raw_val, (str, Path)):
        raise ConfigError(f"{field_name}{ctx} must be a Path or str, got {type(raw_val).__name__}.")
    path = branch(
        isinstance(raw_val, Path),
        lambda: cast(Path, raw_val),
        lambda: Path(cast(str, raw_val).strip()),
    )
    return branch(expand, lambda: expand_path(path), lambda: path)


def resolve_path_within_base_dir(
    path: Path,
    base_dir: Path,
    field_name: str = "path",
    context: str = "",
) -> Path:
    """Resolves path against base_dir and asserts that it resides strictly inside base_dir.

    Args:
        path: Relative or absolute Path to resolve and validate.
        base_dir: Base directory that must contain the resolved path.
        field_name: Name of the configuration field for error reporting.
        context: Optional context suffix (e.g. "for render engine 'jinja'").

    Returns:
        The resolved absolute Path strictly inside base_dir.

    Raises:
        ConfigError: If path is partially anchored, resolves outside base_dir, or equals base_dir itself.
    """
    ctx = branch(bool(context), lambda: f" {context.strip()}", lambda: "")
    if check_is_partially_anchored_path(path):
        raise ConfigError(
            f"{field_name}{ctx} '{path}' cannot resolve outside base directory '{base_dir}'."
        )
    resolved_base = base_dir.resolve()
    resolved_path = branch(bool(path.anchor), lambda: path, lambda: base_dir / path).resolve()
    if not is_relative_to(resolved_path, resolved_base):
        raise ConfigError(
            f"{field_name}{ctx} '{path}' cannot resolve outside base directory '{base_dir}'."
        )
    if resolved_path == resolved_base:
        raise ConfigError(
            f"{field_name}{ctx} '{path}' cannot be the base directory itself."
        )
    return resolved_path


def parse_optional_config_path(
    raw_val: Any,
    field_name: str = "path",
    expand: bool = False,
    dot_as_none: bool = True,
    base_dir: Optional[Path] = None,
    must_be_inside_base: bool = False,
    context: str = "",
) -> Optional[Path]:
    """Coerces an optional configuration value (str or Path) into a Path or None.

    Args:
        raw_val: Raw optional path value (None, str, or Path).
        field_name: Name of the configuration field for error reporting.
        expand: If True, expands '~' and environment variables via expand_path().
        dot_as_none: If True, treats empty strings "" or "." (Path(".")) as None.
        base_dir: Optional base directory to resolve relative paths against.
        must_be_inside_base: If True (requires base_dir), enforces that the resolved path
            resides strictly inside base_dir and is not base_dir itself.
        context: Optional context suffix (e.g. "under [secrets]", "for render engine 'jinja'").

    Returns:
        Coerced Path (or resolved Path against base_dir), or None.

    Raises:
        ConfigError: If raw_val is not None/str/Path, or fails base_dir containment checks.
    """
    def _coerce(val: Any) -> Optional[Path]:
        path = parse_config_path(val, field_name=field_name, expand=expand, context=context)
        if dot_as_none and path == Path("."):
            return None
        if base_dir is None:
            return path
        return branch(
            must_be_inside_base,
            lambda: resolve_path_within_base_dir(path, base_dir, field_name=field_name, context=context),
            lambda: branch(bool(path.anchor), lambda: path, lambda: (base_dir / path).resolve()),
        )

    return when_not_none(raw_val, _coerce)



