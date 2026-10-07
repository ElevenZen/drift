"""Environment variable utilities, secret vault loading, and scoped context managers.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 4: Scoped Activation Context Managers
    env_scope(envs, overwrite, env_keep, mask_values)
        load_env_settings -> update_env_dict(os.environ, ...) [Layer 2]
        yield
        unload_env_settings -> restore_env_dict(os.environ, ...) [Layer 2]

Layer 3: Configuration Resolution & Interpolation Pipeline
    resolve_env_configs(current_layer, lower_layer, package_facts)
        topological_sort_env [Layer 1] (builds reference graph and invokes topological_sort)
        resolve_env_references [Layer 1] (per-tier DAG expansion)
        -> EnvResolve(current, effective, all_facts)

    interpolate_config_dict(data, env, exclude_keys, error_cls)
        python_envsubst [Layer 1] (recursive dict/list/str traversal)

Layer 2: os.environ Mutation Primitives
    load_env_settings(envs, overwrite, env_keep, mask_values) -> EnvSnapshot
        update_env_dict(os.environ, ...) [Layer 1]
    unload_env_settings(original_envs, mask_values)
        restore_env_dict(os.environ, ...) [Layer 1]

Layer 1: Pure Functional Primitives (no os.environ mutation)
    Data Structures:
        EnvConfig(override, secrets, default, fallback)
        EnvImpact(overrides, defaults, secret_keys)
        EnvResolve(current, effective, impact)
        EnvSnapshot = Dict[str, Optional[str]]

    Parsing & Validation:
        parse_env_dict(env_data, context_desc) -> EnvConfig
        parse_env_text(content) -> Dict[str, str]
        parse_env_file(file_path) -> Dict[str, str]
        parse_secrets_env(drift_root) -> Dict[str, str]
        merge_kvpairs(mappings) -> Dict[str, str]

    Reference Resolution:
        topological_sort(graph, error_cls, cycle_msg_prefix) -> List[T]
        topological_sort_env(raw_env, error_cls) -> List[str]
        resolve_env_references(raw_env, base_env, error_cls) -> Dict[str, str]
        python_envsubst(template_content, error_cls, env) -> str

    Dict Manipulation:
        update_env_dict(target, source, overwrite, env_keep, mask_values) -> (target, EnvSnapshot)
        restore_env_dict(target, original_envs, mask_values) -> None
"""

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
    FrozenSet,
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
)
from ..core.exceptions import ConfigError, DriftError, RenderError
from .host_facts import get_cached_system_facts

logger = logging.getLogger(__name__)

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
class EnvImpact:
    """Encapsulates the two-phase mutation contract of configuration on the host environment."""
    overrides: Dict[str, str] = field(default_factory=dict)
    defaults: Dict[str, str] = field(default_factory=dict)
    secret_keys: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def declared_keys(self) -> Set[str]:
        """Returns the closed set of variable names declared across defaults and overrides."""
        return set(self.defaults.keys()) | set(self.overrides.keys())

    def full_env(self, base_env: Mapping[str, str] = os.environ) -> Dict[str, str]:
        """Computes the full environment dictionary overlaying defaults and forced overrides.

        Precedence: Tier 1/2 overrides > Tier 3 base_env > Tier 4/5/6 defaults.
        """
        return {**self.defaults, **base_env, **self.overrides}

    def restricted_env(self, base_env: Mapping[str, str] = os.environ) -> Dict[str, str]:
        """Derives the projected runtime dictionary strictly bounded to declared keys.

        Guarantees zero ambient session noise leakage (no SHLVL, SSH_AUTH_SOCK, etc.),
        while faithfully projecting ambient CLI values that override defaults.
        """
        full = self.full_env(base_env)
        return {k: full[k] for k in self.declared_keys}

    @contextmanager
    def scope(
        self,
        target_env: MutableMapping[str, str] = os.environ,
    ) -> Iterator[None]:
        """Two-phase context manager scoping environment settings into target_env (defaults to os.environ).

        Phase 1: Defaults (Tier 4/5/6) applied conditionally (overwrite=False).
        Phase 2: Overrides (Tier 1/2) applied unconditionally (overwrite=True).
        Restores previous state and unloads additions on exit.
        """
        _, saved_defaults = update_env_dict(
            target_env,
            self.defaults,
            overwrite=False,
            mask_values=self.secret_keys,
        )
        _, saved_overrides = update_env_dict(
            target_env,
            self.overrides,
            overwrite=True,
            mask_values=self.secret_keys,
        )
        try:
            yield
        finally:
            restore_env_dict(target_env, saved_overrides, mask_values=self.secret_keys)
            restore_env_dict(target_env, saved_defaults, mask_values=self.secret_keys)


@dataclass(frozen=True)
class EnvResolve:
    """Holds layer-local environment definitions, cumulative effective tables, and two-phase impact."""
    current: EnvConfig = field(default_factory=EnvConfig)
    effective: EnvConfig = field(default_factory=EnvConfig)
    impact: EnvImpact = field(default_factory=EnvImpact)

    def __init__(
        self,
        current: Optional[EnvConfig] = None,
        effective: Optional[EnvConfig] = None,
        all_facts: Optional[Mapping[str, str]] = None,
        impact: Optional[EnvImpact] = None,
    ):
        curr = current or EnvConfig()
        eff = effective or EnvConfig()
        object.__setattr__(self, "current", curr)
        object.__setattr__(self, "effective", eff)
        imp = impact if impact is not None else self.build_impact(all_facts=all_facts)
        object.__setattr__(self, "impact", imp)

    def build_impact(
        self,
        all_facts: Optional[Mapping[str, str]] = None,
    ) -> EnvImpact:
        """Constructs an EnvImpact from the effective tables and authoritative facts following 6-Tier Precedence."""
        defaults: Dict[str, str] = dict(self.effective.fallback)
        defaults.update(self.effective.default)
        defaults.update(self.effective.secrets)

        overrides: Dict[str, str] = dict(all_facts or {})
        overrides.update(self.effective.override)

        # Maintain mutual disjointness: overrides take precedence over defaults
        disjoint_defaults = {k: v for k, v in defaults.items() if k not in overrides}

        return EnvImpact(
            overrides=overrides,
            defaults=disjoint_defaults,
            secret_keys=frozenset(self.effective.secrets.keys()),
        )

    @property
    def secret_keys(self) -> Set[str]:
        """Returns the set of all effective secret variable names."""
        return set(self.impact.secret_keys)


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


def merge_kvpairs(mappings: Iterable[Mapping[str, str]]) -> Dict[str, str]:
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


def resolve_env_configs(
    current_layer: EnvConfig,
    lower_layer: Optional[EnvConfig] = None,
    package_facts: Optional[Mapping[str, str]] = None,
) -> EnvResolve:
    """Evaluates the 6-Tier Precedence Model and Kahn's DAG topological sorting.

    6-Tier Precedence Hierarchy (Package > Workspace within each tier):
    - Tier 1: Override (Package [env.override] > Workspace [env.override])
    - Tier 2: Facts (Package Facts > System facts)
    - Tier 3: CLI & Ambient Process Context (os.environ / CLI arguments)
    - Tier 4: Secrets (Package [env.secrets] > Workspace [env.secrets] > secrets_file)
    - Tier 5: Default (Package [env.default] > Workspace [env.default])
    - Tier 6: Fallback (Package [env.fallback] > Workspace [env.fallback])

    Returns:
        EnvResolve containing current layer tables, cumulative effective tables, and two-phase EnvImpact.
    """
    lower = lower_layer or EnvConfig()
    system_facts = get_cached_system_facts()
    all_facts = {**system_facts, **(package_facts or {})}
    protected_facts = frozenset(all_facts.keys())

    # Seed resolution base from host os.environ, with Tier 2 facts taking precedence over Tier 3 ambient context
    base_env: Dict[str, str] = {**os.environ, **all_facts}

    # 1. Tier 4 (Secrets): Target secrets > Lower secrets
    secrets_base = dict(base_env)
    update_env_dict(secrets_base, lower.secrets, overwrite=True, env_keep=protected_facts, mask_values=True)
    resolved_secrets = resolve_env_references(current_layer.secrets, base_env=secrets_base, error_cls=ConfigError) if current_layer.secrets else {}
    effective_secrets = {**lower.secrets, **resolved_secrets}

    # 2. Tier 6 (Fallback): Target fallback > Lower fallback (can reference secrets)
    fallback_base = dict(secrets_base)
    update_env_dict(fallback_base, effective_secrets, overwrite=True, env_keep=protected_facts)
    update_env_dict(fallback_base, lower.fallback, overwrite=False)
    resolved_fallback = resolve_env_references(current_layer.fallback, base_env=fallback_base, error_cls=ConfigError) if current_layer.fallback else {}
    effective_fallback = {**lower.fallback, **resolved_fallback}

    # 3. Tier 5 (Default): Target default > Lower default (can reference secrets + fallback)
    default_base = dict(fallback_base)
    update_env_dict(default_base, effective_fallback, overwrite=True, env_keep=protected_facts)
    update_env_dict(default_base, lower.default, overwrite=True, env_keep=protected_facts)
    resolved_default = resolve_env_references(current_layer.default, base_env=default_base, error_cls=ConfigError) if current_layer.default else {}
    effective_default = {**lower.default, **resolved_default}

    # 4. Tier 1 (Override): Target override > Lower override (can reference all)
    override_base = dict(default_base)
    update_env_dict(override_base, effective_default, overwrite=True, env_keep=protected_facts)
    update_env_dict(override_base, lower.override, overwrite=True)
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
    return EnvResolve(
        current=current,
        effective=effective,
        all_facts=all_facts,
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

    def replace_var(match: re.Match) -> str:
        escaped, braced_var, plain_var = match.group(1), match.group(2), match.group(3)
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

    return ENV_VAR_PATTERN.sub(replace_var, template_content)


# =====================================================================
# Variable Reference Extraction
# =====================================================================

ENV_VAR_PATTERN = re.compile(r"(\\)?\$(?:\{([a-zA-Z_][a-zA-Z0-9_]*)\}|([a-zA-Z_][a-zA-Z0-9_]*))")
"""Unified pattern matching $VAR / ${VAR} references with optional backslash escape group.

Capture groups:
    1: Backslash escape prefix (if present, the reference is literal)
    2: Braced variable name (e.g. 'FOO' from '${FOO}')
    3: Plain variable name (e.g. 'FOO' from '$FOO')
"""


def _extract_var_refs(value: str) -> Set[str]:
    """Extracts the set of unescaped variable names referenced in a value string."""
    return {
        braced or plain
        for escaped, braced, plain in ENV_VAR_PATTERN.findall(value)
        if not escaped
    }


T = TypeVar("T")


def topological_sort(
    graph: Mapping[T, Set[T]],
    error_cls: Type[Exception] = ValueError,
    cycle_msg_prefix: str = "Cyclic dependency detected",
) -> List[T]:
    """Computes a topologically sorted evaluation order for a directed acyclic graph (DAG) using Kahn's algorithm.

    In this graph representation, graph[node] is the set of prerequisite dependencies that must
    be processed before node.

    Args:
        graph: Mapping from each node to the set of nodes it depends on (prerequisites).
        error_cls: Exception class to raise if a cyclic dependency or immediate self-reference is detected.
        cycle_msg_prefix: Prefix for exception message on cyclic dependency detection.

    Returns:
        List of nodes in valid topological evaluation order (prerequisites appear before dependents).

    Raises:
        error_cls: If one or more cycles are detected.
    """
    raw_keys = set(graph.keys())

    # 1. Check for immediate self-reference
    for node, deps in graph.items():
        if node in deps:
            raise error_cls(f"{cycle_msg_prefix}: '{node}' references itself.")

    # 2. In-degree represents the count of unmet prerequisites within the graph
    in_degree: Dict[T, int] = {
        node: len(deps & raw_keys)
        for node, deps in graph.items()
    }

    # 3. Map each prerequisite to the set of nodes that depend on it
    dependents: Dict[T, Set[T]] = {k: set() for k in raw_keys}
    for node, deps in graph.items():
        for dep in (deps & raw_keys):
            dependents[dep].add(node)

    # 4. Topological sort using Kahn's algorithm with min-heap for deterministic tie-breaking
    import heapq

    queue: List[T] = []
    heap: List[Any] = []
    try:
        heap = [k for k, deg in in_degree.items() if deg == 0]
        heapq.heapify(heap)
        use_heap = True
    except TypeError:
        # Fallback for unorderable node types
        queue = [k for k, deg in in_degree.items() if deg == 0]
        use_heap = False

    eval_order: List[T] = []

    if use_heap:
        while heap:
            curr: T = heapq.heappop(heap)
            eval_order.append(curr)
            try:
                deps_iter = sorted(dependents[curr])  # type: ignore[type-var]
            except TypeError:
                deps_iter = list(dependents[curr])
            for dep in deps_iter:
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    heapq.heappush(heap, dep)
    else:
        while queue:
            curr = queue.pop(0)
            eval_order.append(curr)
            for dep in dependents[curr]:
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    queue.append(dep)

    if len(eval_order) != len(raw_keys):
        cyclic_keys = sorted([str(k) for k, deg in in_degree.items() if deg > 0])
        raise error_cls(
            f"{cycle_msg_prefix} among: {', '.join(cyclic_keys)}"
        )

    return eval_order


def topological_sort_env(
    raw_env: Mapping[str, str],
    error_cls: Type[DriftError] = ConfigError,
) -> List[str]:
    """Computes a topologically sorted evaluation order for environment variables using Kahn's algorithm.

    Identifies variable references within raw_env keys (e.g. $VAR, ${VAR}), ignoring escaped
    variables (e.g. \\$VAR), and returns a list of keys in valid evaluation order.

    Args:
        raw_env: Mapping of environment variable names to string values.
        error_cls: Exception class to raise on cyclic dependencies or immediate self-references.

    Returns:
        List of environment variable keys in topological evaluation order.

    Raises:
        error_cls: If an immediate self-reference or cyclic dependency is detected.
    """
    raw_keys = set(raw_env.keys())

    # Build dependency graph: each key maps to internal dependencies within raw_env
    graph: Dict[str, Set[str]] = {
        k: _extract_var_refs(v) & raw_keys
        for k, v in raw_env.items()
    }

    return topological_sort(
        graph,
        error_cls=error_cls,
        cycle_msg_prefix="Cyclic dependency detected in environment variables",
    )


def resolve_env_references(
    raw_env: Mapping[str, str],
    base_env: Mapping[str, str],
    error_cls: Type[DriftError] = ConfigError,
) -> Dict[str, str]:
    """Resolves inter-variable references in an environment dictionary using topological sorting.

    Variables can reference other variables in raw_env, as well as external base_env.
    Any referenced variable missing from both raw_env and base_env will trigger error_cls.
    Cyclic dependencies will also trigger error_cls.

    Args:
        raw_env: Mapping of raw environment variable definitions to strings.
        base_env: Required base environment mapping.
        error_cls: Exception class to raise on error (defaults to ConfigError).

    Returns:
        A dictionary of fully resolved, string-valued environment variables.

    Raises:
        error_cls: If any referenced variable is missing or if a cyclic dependency is detected.
    """
    eval_order = topological_sort_env(raw_env, error_cls=error_cls)

    # Gradual evaluation in topological order
    resolved: Dict[str, str] = dict(base_env)
    result: Dict[str, str] = {}
    for k in eval_order:
        rendered_val = python_envsubst(raw_env[k], env=resolved, error_cls=error_cls)
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
            return python_envsubst(data, env=env, error_cls=error_cls)
        return data
    else:
        return data
