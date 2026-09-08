"""Environment variable utilities, secret vault loading, and scoped context managers."""

import os
import re
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple, Union, Iterable, Any, Type

from .constants import (
    CONFIG_DIR_NAME,
    SECRETS_ENV_FILE_NAME,
    INITIAL_ENV,
)
from .exceptions import ConfigError, DriftError, RenderError

logger = logging.getLogger(__name__)

EnvInput = Union[Mapping[str, str], Sequence[Tuple[str, str]]]
EnvSnapshot = Dict[str, Optional[str]]


def parse_env_text(content: str) -> Dict[str, str]:
    """Parses standard key-value lines (stripping comments and quotes)."""
    parsed: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            if (k.startswith('"') and k.endswith('"')) or (k.startswith("'") and k.endswith("'")):
                k = k[1:-1]
            parsed[k] = v
    return parsed


def parse_env_file(file_path: Path) -> Dict[str, str]:
    """Parses a key-value env file if present, returning a dictionary."""
    if not file_path.is_file():
        return {}
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        return parse_env_text(content)
    except Exception as e:
        logger.warning(f"Failed to read env file at '{file_path}': {e}")
        return {}


def parse_secrets_env(drift_root: Path) -> Dict[str, str]:
    """Reads and parses the config/secrets.env file into a dictionary."""
    secrets_file = Path(drift_root) / CONFIG_DIR_NAME / SECRETS_ENV_FILE_NAME
    return parse_env_file(secrets_file)



def update_env_dict(
    target: MutableMapping[str, str],
    source: Optional[Union[Mapping[str, Any], Sequence[Tuple[str, Any]]]],
    overwrite: bool = True,
    env_keep: Optional[Union[Set[str], Sequence[str], Iterable[str]]] = None,
) -> Tuple[MutableMapping[str, str], EnvSnapshot]:
    """Updates target environment mapping with key-value pairs from source.

    Args:
        target: The mutable target mapping (e.g. dict or os.environ) to update in-place.
        source: A mapping or sequence of (key, value) pairs to apply.
        overwrite: If True, overwrites existing keys in target unless protected by env_keep.
                   If False, only sets keys that are currently unset in target.
        env_keep: Optional set/iterable of variable names protected from being overwritten.

    Returns:
        A tuple of (target, saved_envs) where saved_envs maps modified keys to their previous
        value in target (None if previously unset).
    """
    if not source:
        return target, {}

    items = source.items() if isinstance(source, Mapping) else source
    if env_keep is None:
        keep_set: Set[str] = set()
    elif isinstance(env_keep, set):
        keep_set = env_keep
    else:
        keep_set = set(env_keep)

    saved: EnvSnapshot = {}

    for k, v in items:
        k_str = str(k)
        v_str = str(v)
        if k_str in keep_set and k_str in target:
            logger.debug(f"Environment variable skipped (in env_keep): {k_str}")
            continue
        if not overwrite and k_str in target:
            logger.debug(f"Environment variable skipped (already set and overwrite=False): {k_str}")
            continue

        if k_str not in saved:
            saved[k_str] = target.get(k_str)
        target[k_str] = v_str
        logger.debug(f"Environment variable loaded: {k_str}={v_str}")

    return target, saved


def restore_env_dict(
    target: MutableMapping[str, str],
    original_envs: Optional[Mapping[str, Optional[str]]],
) -> None:
    """Restores original values in target mapping from a snapshot dictionary.

    Args:
        target: The mutable target mapping (e.g. dict or os.environ) to restore in-place.
        original_envs: A snapshot dictionary mapping keys to their original values (None if unset).
    """
    if not original_envs:
        return

    for k, original_val in original_envs.items():
        if original_val is None:
            target.pop(k, None)
            logger.debug(f"Environment variable unloaded: popped {k}")
        else:
            target[k] = original_val
            logger.debug(f"Environment variable unloaded: restored {k}={original_val}")


def load_env_settings(
    envs: Optional[EnvInput],
    overwrite: bool = True,
    env_keep: Optional[Union[Set[str], Sequence[str], Iterable[str]]] = None,
) -> EnvSnapshot:
    """Loads environment settings into os.environ.

    Args:
        envs: A mapping or sequence of (key, value) pairs.
        overwrite: If True, overwrite existing keys in os.environ (unless in env_keep).
                   If False, do not overwrite any keys already in os.environ.
        env_keep: Optional set or sequence of keys that must NOT be overwritten.

    Returns:
        Dict[str, Optional[str]]: A dictionary of modified keys mapped to their original values
                                  (None if the key was previously unset in os.environ).
    """
    _, saved = update_env_dict(os.environ, envs, overwrite=overwrite, env_keep=env_keep)
    return saved


def unload_env_settings(original_envs: Optional[Mapping[str, Optional[str]]]) -> None:
    """Restores the original environment values using the snapshot returned by load_env_settings."""
    restore_env_dict(os.environ, original_envs)


@contextmanager
def env_scope(
    envs: Optional[EnvInput],
    overwrite: bool = True,
    env_keep: Optional[Union[Set[str], Sequence[str], Iterable[str]]] = None,
) -> Iterator[None]:
    """Context manager for loading and unloading environment settings."""
    saved_envs = load_env_settings(envs, overwrite=overwrite, env_keep=env_keep)
    try:
        yield
    finally:
        unload_env_settings(saved_envs)


@contextmanager
def secrets_env_scope(drift_root: Path) -> Iterator[None]:
    """Context manager for loading secrets from secrets.env into os.environ."""
    secrets = parse_secrets_env(drift_root)
    with env_scope(secrets, overwrite=True, env_keep=INITIAL_ENV):
        yield


def python_envsubst(
    template_content: str,
    error_cls: Type[DriftError],
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Pure-Python envsubst equivalent for platforms without GNU gettext or as fallback.

    Supports escaping with backslash (e.g. \\$VAR or \\${VAR}) to output literal $VAR or ${VAR}
    without variable substitution.

    Args:
        template_content: Raw template string containing $VAR or ${VAR}.
        error_cls: Exception class to raise on missing variable (e.g. RenderError or ConfigError).
        env: Optional dictionary of environment variables (defaults to os.environ).

    Returns:
        The rendered template content as a string.

    Raises:
        error_cls: If any unescaped referenced variable is not defined in the environment.
    """
    environ = env if env is not None else os.environ
    pattern = re.compile(r"(\\)?\$(?:\{([a-zA-Z_][a-zA-Z0-9_]*)\}|([a-zA-Z_][a-zA-Z0-9_]*))")

    def replace_var(match: re.Match) -> str:
        escaped = match.group(1)
        braced_var = match.group(2)
        plain_var = match.group(3)
        var_name = braced_var or plain_var

        if escaped:
            # Escaped: strip leading backslash and preserve literal variable syntax
            if braced_var:
                return f"${{{braced_var}}}"
            return f"${plain_var}"

        if var_name not in environ:
            raise error_cls(
                f"Environment variable '${var_name}' referenced in template "
                f"was not found in [env], secrets.env, or process environment."
            )
        return str(environ[var_name])

    return pattern.sub(replace_var, template_content)


VAR_PATTERN = re.compile(r"(?<!\\)\$(?:\{([a-zA-Z_][a-zA-Z0-9_]*)\}|([a-zA-Z_][a-zA-Z0-9_]*))")


def topological_sort_env(
    raw_env: Mapping[str, Any],
    error_cls: Type[DriftError] = ConfigError,
) -> List[str]:
    """Computes a topologically sorted evaluation order for environment variables using Kahn's algorithm.

    Identifies variable references within raw_env keys (e.g. $VAR, ${VAR}), ignoring escaped
    variables (e.g. \\$VAR), and returns a list of keys in valid evaluation order.

    Args:
        raw_env: Mapping of environment variable names to raw (possibly unexpanded) values.
        error_cls: Exception class to raise on cyclic dependencies or immediate self-references.

    Returns:
        List of environment variable keys in topological evaluation order.

    Raises:
        error_cls: If an immediate self-reference or cyclic dependency is detected.
    """
    raw_dict = {str(k): str(v) for k, v in raw_env.items()}
    raw_keys = set(raw_dict.keys())

    # 1. Build dependency graph (only tracking internal dependencies within raw_dict)
    graph: Dict[str, Set[str]] = {}
    in_degree: Dict[str, int] = {}

    for k, v in raw_dict.items():
        refs = {m[0] or m[1] for m in VAR_PATTERN.findall(v)}
        # Check for immediate self-reference
        if k in refs:
            raise error_cls(f"Cyclic dependency detected in [env] variable: '{k}' references itself.")
        # Check for reference intersection with keys in raw_env
        internal_deps = refs & raw_keys
        graph[k] = internal_deps
        in_degree[k] = len(internal_deps)

    # 2. Topological sort using Kahn's algorithm
    queue = [k for k, deg in in_degree.items() if deg == 0]
    eval_order: List[str] = []

    # Map each key to the set of keys that depend on it
    dependents: Dict[str, Set[str]] = {k: set() for k in raw_keys}
    for k, deps in graph.items():
        for dep in deps:
            dependents[dep].add(k)

    while queue:
        curr = queue.pop(0)
        eval_order.append(curr)
        for dep in dependents[curr]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    if len(eval_order) != len(raw_keys):
        cyclic_keys = sorted([k for k, deg in in_degree.items() if deg > 0])
        raise error_cls(
            f"Cyclic dependency detected in [env] variables among: {', '.join(cyclic_keys)}"
        )

    return eval_order


def resolve_env_references(
    raw_env: Mapping[str, Any],
    base_env: Mapping[str, str],
    error_cls: Type[DriftError] = ConfigError,
) -> Dict[str, str]:
    """Resolves inter-variable references in an [env] dictionary using topological sorting.

    Variables can reference other variables in raw_env, as well as external base_env.
    Any referenced variable missing from both raw_env and base_env will trigger error_cls.
    Cyclic dependencies will also trigger error_cls.

    Args:
        raw_env: Dictionary of raw environment variable definitions.
        base_env: Required base environment mapping.
        error_cls: Exception class to raise on error (defaults to ConfigError).

    Returns:
        A dictionary of fully resolved, string-valued environment variables.

    Raises:
        error_cls: If any referenced variable is missing or if a cyclic dependency is detected.
    """
    raw_dict = {str(k): str(v) for k, v in raw_env.items()}
    eval_order = topological_sort_env(raw_dict, error_cls=error_cls)

    # Gradual evaluation in topological order
    resolved: Dict[str, str] = dict(base_env)
    result: Dict[str, str] = {}
    for k in eval_order:
        rendered_val = python_envsubst(raw_dict[k], env=resolved, error_cls=error_cls)
        resolved[k] = rendered_val
        result[k] = rendered_val

    return result


def interpolate_config_dict(
    data: Any,
    env: Mapping[str, str],
    exclude_keys: Optional[Set[str]] = None,
    error_cls: Type[DriftError] = ConfigError,
) -> Any:
    """Recursively interpolates ${VAR} strings in a parsed TOML dictionary structure.

    Args:
        data: The parsed configuration structure (dict, list, string, etc.).
        env: The resolved environment variable mapping.
        exclude_keys: Optional set of dictionary keys to skip from interpolation (e.g. {'env'}).
        error_cls: Exception class to raise if an unresolvable variable is encountered.

    Returns:
        The structure with all string values interpolated.

    Raises:
        error_cls: If any unescaped referenced variable is missing from the environment.
    """
    excluded = exclude_keys or set()

    if isinstance(data, dict):
        new_dict = {}
        for k, v in data.items():
            if k in excluded:
                new_dict[k] = v
            else:
                new_dict[k] = interpolate_config_dict(v, env=env, exclude_keys=None, error_cls=error_cls)
        return new_dict
    elif isinstance(data, list):
        return [interpolate_config_dict(item, env=env, exclude_keys=None, error_cls=error_cls) for item in data]
    elif isinstance(data, tuple):
        return tuple(interpolate_config_dict(item, env=env, exclude_keys=None, error_cls=error_cls) for item in data)
    elif isinstance(data, str):
        if "$" in data:
            return python_envsubst(data, env=dict(env), error_cls=error_cls)
        return data
    else:
        return data
