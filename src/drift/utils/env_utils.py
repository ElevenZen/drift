"""Environment variable utilities, secret vault loading, and scoped context managers."""

import os
import re
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Dict,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Set,
    Tuple,
    Union,
    Iterable,
    Any,
    Type,
    TypeVar,
)

from ..core.constants import (
    CONFIG_DIR_NAME,
    SECRETS_ENV_FILE_NAME,
    INITIAL_ENV,
    DRIFT_SYSTEM_FACT_KEYS,
)
from ..core.exceptions import ConfigError, DriftError, RenderError

logger = logging.getLogger(__name__)

K = TypeVar("K")
V = TypeVar("V")

EnvInput = Union[Mapping[str, str], Iterable[Tuple[str, str]]]
EnvSnapshot = Dict[str, Optional[str]]


@dataclass(frozen=True)
class EnvConfig:
    """Structured container holding 4 raw/resolved environment tables."""
    override: Dict[str, str] = field(default_factory=dict)
    secrets: Dict[str, str] = field(default_factory=dict)
    default: Dict[str, str] = field(default_factory=dict)
    fallback: Dict[str, str] = field(default_factory=dict)

    def to_env_dict(self) -> Dict[str, Any]:
        """Serializes environment tables for TOML dump using pure functional mapping."""
        tables = {
            "override": self.override,
            "secrets": self.secrets,
            "default": self.default,
            "fallback": self.fallback,
        }
        return {k: dict(v) for k, v in tables.items() if v}


@dataclass(frozen=True)
class EnvResolve:
    """Holds layer-local environment definitions, cumulative effective tables, and flattened runtime dictionary."""
    current: EnvConfig = field(default_factory=EnvConfig)
    effective: EnvConfig = field(default_factory=EnvConfig)
    effective_dict: Dict[str, str] = field(default_factory=dict)

    @property
    def secret_keys(self) -> Set[str]:
        """Returns the set of all effective secret variable names."""
        return set(self.effective.secrets.keys())


ENV_SUBTABLE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "override": ("override", "overwrite"),
    "secrets": ("secrets",),
    "default": ("default",),
    "fallback": ("fallback",),
}


def _extract_env_subtable(
    table_name: str,
    table_data: Any,
    context_desc: str,
) -> Dict[str, str]:
    """Helper to validate and extract stringified key-value pairs from an env sub-table."""
    if not isinstance(table_data, dict):
        raise ConfigError(f"[env.{table_name}] must be a table of key-value pairs in {context_desc}.")
    return {str(k): str(v) for k, v in table_data.items()}


def merge_kvpairs(mappings: Iterable[Mapping[K, V]]) -> Dict[K, V]:
    """Pure functional helper to merge multiple key-value mappings into a single dictionary."""
    return {k: v for m in mappings for k, v in m.items()}


def parse_env_dict(
    env_data: Any,
    context_desc: str = "workspace configuration",
) -> EnvConfig:
    """Parses environment configuration tables into an EnvConfig instance.

    Supports [env.override] (and alias [env.overwrite]), [env.secrets], [env.default], and [env.fallback].
    Rejects flat key-value pairs or unknown sub-tables with ConfigError.
    """
    if not env_data:
        return EnvConfig()
    if not isinstance(env_data, dict):
        raise ConfigError(f"[env] section must be a table in {context_desc}.")

    valid_keys = set().union(*ENV_SUBTABLE_ALIASES.values())
    invalid_keys = [k for k in env_data if k not in valid_keys]
    if invalid_keys:
        raise ConfigError(
            f"Unsupported key '{invalid_keys[0]}' in [env] in {context_desc}. "
            f"Expected [env.override], [env.secrets], [env.default], or [env.fallback]."
        )

    extracted = {
        target: merge_kvpairs(
            _extract_env_subtable(alias, env_data[alias], context_desc)
            for alias in aliases
            if alias in env_data
        )
        for target, aliases in ENV_SUBTABLE_ALIASES.items()
    }

    return EnvConfig(
        override=extracted["override"],
        secrets=extracted["secrets"],
        default=extracted["default"],
        fallback=extracted["fallback"],
    )


def build_effective_env_dict(
    env_config: EnvConfig,
    extra_facts: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Flattens an EnvConfig into an effective environment dictionary following 6-Tier Precedence."""
    protected_facts = set(DRIFT_SYSTEM_FACT_KEYS) | set((extra_facts or {}).keys())
    effective_env = dict(env_config.fallback)
    update_env_dict(effective_env, env_config.default, overwrite=True, env_keep=protected_facts)
    update_env_dict(effective_env, env_config.secrets, overwrite=True, env_keep=protected_facts, mask_values=True)
    update_env_dict(effective_env, extra_facts or {}, overwrite=True)
    update_env_dict(effective_env, env_config.override, overwrite=True)
    return effective_env


def resolve_env_configs(
    current_layer: EnvConfig,
    lower_layer: Optional[EnvConfig] = None,
    extra_facts: Optional[Mapping[str, str]] = None,
) -> EnvResolve:
    """Evaluates the 6-Tier Precedence Model and Kahn's DAG topological sorting.

    6-Tier Precedence Hierarchy (Package > Workspace within each tier):
    - Tier 1: CLI / Ambient Process Context (INITIAL_ENV / os.environ)
    - Tier 2: Override (Package [env.override] > Workspace [env.override])
    - Tier 3: Facts (Package Facts > System facts)
    - Tier 4: Secrets (Package [env.secrets] > Workspace [env.secrets] > secrets_file)
    - Tier 5: Default (Package [env.default] > Workspace [env.default])
    - Tier 6: Fallback (Package [env.fallback] > Workspace [env.fallback])

    Returns:
        EnvResolve containing current layer tables, cumulative effective tables, and flattened runtime dictionary.
    """
    lower = lower_layer or EnvConfig()
    protected_facts = set(INITIAL_ENV) | set(DRIFT_SYSTEM_FACT_KEYS) | set((extra_facts or {}).keys())
    base_env, _ = update_env_dict(dict(os.environ), extra_facts or {}, overwrite=True, env_keep=set(INITIAL_ENV))

    # 1. Tier 4 (Secrets): Target secrets > Lower secrets
    secrets_base, _ = update_env_dict(dict(base_env), lower.secrets,
                                      overwrite=True, env_keep=protected_facts, mask_values=True)
    resolved_secrets = resolve_env_references(current_layer.secrets, base_env=secrets_base, error_cls=ConfigError) if current_layer.secrets else {}
    effective_secrets = {**lower.secrets, **resolved_secrets}

    # 2. Tier 6 (Fallback): Target fallback > Lower fallback (can reference secrets)
    fallback_base, _ = update_env_dict(dict(secrets_base), effective_secrets, overwrite=True, env_keep=protected_facts)
    fallback_base, _ = update_env_dict(dict(fallback_base), lower.fallback, overwrite=False)
    resolved_fallback = resolve_env_references(current_layer.fallback, base_env=fallback_base, error_cls=ConfigError) if current_layer.fallback else {}
    effective_fallback = {**lower.fallback, **resolved_fallback}

    # 3. Tier 5 (Default): Target default > Lower default (can reference secrets + fallback)
    default_base, _ = update_env_dict(dict(fallback_base), effective_fallback, overwrite=True, env_keep=protected_facts)
    default_base, _ = update_env_dict(dict(default_base), lower.default, overwrite=True, env_keep=protected_facts)
    resolved_default = resolve_env_references(current_layer.default, base_env=default_base, error_cls=ConfigError) if current_layer.default else {}
    effective_default = {**lower.default, **resolved_default}

    # 4. Tier 2 (Override): Target override > Lower override (can reference all)
    override_base, _ = update_env_dict(dict(default_base), effective_default, overwrite=True, env_keep=protected_facts)
    override_base, _ = update_env_dict(dict(override_base), lower.override, overwrite=True, env_keep=set(INITIAL_ENV))
    resolved_override = resolve_env_references(current_layer.override, base_env=override_base, error_cls=ConfigError) if current_layer.override else {}
    effective_override = {**lower.override, **resolved_override}

    current = EnvConfig(
        override=resolved_override,
        secrets=resolved_secrets,
        default=resolved_default,
        fallback=resolved_fallback,
    )
    effective = EnvConfig(
        override=effective_override,
        secrets=effective_secrets,
        default=effective_default,
        fallback=effective_fallback,
    )
    effective_dict = build_effective_env_dict(effective, extra_facts=extra_facts)

    return EnvResolve(
        current=current,
        effective=effective,
        effective_dict=effective_dict,
    )


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
    source: Optional[Union[Mapping[str, Any], Iterable[Tuple[str, Any]]]],
    overwrite: bool = True,
    env_keep: Optional[Iterable[str]] = None,
    mask_values: Union[bool, Iterable[str]] = False,
) -> Tuple[MutableMapping[str, str], EnvSnapshot]:
    """Updates target environment mapping with key-value pairs from source.

    Args:
        target: The mutable target mapping (e.g. dict or os.environ) to update in-place.
        source: A mapping or iterable of (key, value) pairs to apply.
        overwrite: If True, overwrites existing keys in target unless protected by env_keep.
                   If False, only sets keys that are currently unset in target.
        env_keep: Optional set/iterable of variable names protected from being overwritten.
        mask_values: If True, masks all variable values in debug logs. If an iterable of keys,
                     masks only those specific keys (e.g. key=****).

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

    mask_all = mask_values is True
    mask_set = set(mask_values) if (mask_values and not isinstance(mask_values, bool)) else set()

    saved: EnvSnapshot = {}
    is_os_environ = target is os.environ

    for k, v in items:
        k_str = str(k)
        v_str = str(v)
        if k_str in keep_set and k_str in target:
            if is_os_environ:
                logger.debug(f"Environment variable skipped (in env_keep): {k_str}")
            continue
        if not overwrite and k_str in target:
            if is_os_environ:
                logger.debug(f"Environment variable skipped (already set and overwrite=False): {k_str}")
            continue

        existing_val = target.get(k_str)
        is_new = k_str not in target
        is_overwritten = not is_new and existing_val != v_str

        if k_str not in saved:
            saved[k_str] = existing_val
        target[k_str] = v_str

        if (is_new or is_overwritten) and is_os_environ:
            display_val = "****" if (mask_all or k_str in mask_set) else v_str
            logger.debug(f"Environment variable loaded: {k_str}={display_val}")

    return target, saved


def restore_env_dict(
    target: MutableMapping[str, str],
    original_envs: Mapping[str, Optional[str]] = {},
    mask_values: Union[bool, Iterable[str]] = False,
) -> None:
    """Restores original values in target mapping from a snapshot dictionary.

    Args:
        target: The mutable target mapping (e.g. dict or os.environ) to restore in-place.
        original_envs: A snapshot dictionary mapping keys to their original values (None if unset).
        mask_values: If True, masks all restored values in debug logs. If an iterable of keys,
                     masks only those specific keys (e.g. restored key=****).
    """
    if not original_envs:
        return

    mask_all = mask_values is True
    mask_set = set(mask_values) if (mask_values and not isinstance(mask_values, bool)) else set()

    is_os_environ = target is os.environ
    for k, original_val in original_envs.items():
        if original_val is None:
            if k in target:
                target.pop(k, None)
                if is_os_environ:
                    logger.debug(f"Environment variable unloaded: popped {k}")
        else:
            current_val = target.get(k)
            target[k] = original_val
            if current_val != original_val:
                display_val = "****" if (mask_all or k in mask_set) else original_val
                if is_os_environ:
                    logger.debug(f"Environment variable unloaded: restored {k}={display_val}")


def load_env_settings(
    envs: Optional[EnvInput],
    overwrite: bool = True,
    env_keep: Optional[Iterable[str]] = None,
    mask_values: Union[bool, Iterable[str]] = False,
) -> EnvSnapshot:
    """Loads environment settings into os.environ.

    Args:
        envs: A mapping or iterable of (key, value) pairs.
        overwrite: If True, overwrite existing keys in os.environ (unless in env_keep).
                    If False, do not overwrite any keys already in os.environ.
        env_keep: Optional set or iterable of keys that must NOT be overwritten.
        mask_values: If True or iterable of keys, masks variable values in debug logs (e.g. key=****).

    Returns:
        Dict[str, Optional[str]]: A dictionary of modified keys mapped to their original values
                                  (None if the key was previously unset in os.environ).
    """
    _, saved = update_env_dict(os.environ, envs, overwrite=overwrite, env_keep=env_keep, mask_values=mask_values)
    return saved


def unload_env_settings(
    original_envs: Mapping[str, Optional[str]] = {},
    mask_values: Union[bool, Iterable[str]] = False,
) -> None:
    """Restores the original environment values using the snapshot returned by load_env_settings."""
    restore_env_dict(os.environ, original_envs, mask_values=mask_values)


@contextmanager
def env_scope(
    envs: Optional[EnvInput],
    overwrite: bool = True,
    env_keep: Optional[Iterable[str]] = None,
    mask_values: Union[bool, Iterable[str]] = False,
) -> Iterator[None]:
    """Context manager for loading and unloading environment settings."""
    saved_envs = load_env_settings(envs, overwrite=overwrite, env_keep=env_keep, mask_values=mask_values)
    try:
        yield
    finally:
        unload_env_settings(saved_envs, mask_values=mask_values)


@contextmanager
def env_resolve_scope(
    env_resolve: EnvResolve,
    overwrite: bool = True,
    env_keep: Iterable[str] = INITIAL_ENV,
) -> Iterator[None]:
    """Context manager scoping an EnvResolve instance into os.environ with granular secret masking."""
    with env_scope(
        env_resolve.effective_dict,
        overwrite=overwrite,
        env_keep=env_keep,
        mask_values=env_resolve.secret_keys,
    ):
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
                f"was not found in environment tables, secrets.env, or process environment."
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
            raise error_cls(f"Cyclic dependency detected in environment variable: '{k}' references itself.")
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
            f"Cyclic dependency detected in environment variables among: {', '.join(cyclic_keys)}"
        )

    return eval_order


def resolve_env_references(
    raw_env: Mapping[str, Any],
    base_env: Mapping[str, str],
    error_cls: Type[DriftError] = ConfigError,
) -> Dict[str, str]:
    """Resolves inter-variable references in an environment dictionary using topological sorting.

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
