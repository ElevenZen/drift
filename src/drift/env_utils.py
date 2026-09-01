"""Environment variable utilities, secret vault loading, and scoped context managers."""

import os
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple, Union, Iterable

from .constants import (
    CONFIG_DIR_NAME,
    SECRETS_ENV_FILE_NAME,
    INITIAL_ENV,
)

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
    if not envs:
        return {}

    items = envs.items() if isinstance(envs, Mapping) else envs
    if env_keep is None:
        keep_set: Set[str] = set()
    elif isinstance(env_keep, set):
        keep_set = env_keep
    else:
        keep_set = set(env_keep)

    saved: EnvSnapshot = {}

    for k, v in items:
        if k in keep_set:
            logger.debug(f"Environment variable skipped (in env_keep): {k}")
            continue
        if not overwrite and k in os.environ:
            logger.debug(f"Environment variable skipped (already set and overwrite=False): {k}")
            continue

        if k not in saved:
            saved[k] = os.environ.get(k)
        os.environ[k] = str(v)
        logger.debug(f"Environment variable loaded: {k}={v}")

    return saved


def unload_env_settings(original_envs: Optional[Mapping[str, Optional[str]]]) -> None:
    """Restores the original environment values using the snapshot returned by load_env_settings."""
    if not original_envs:
        return

    for k, original_val in original_envs.items():
        if original_val is None:
            os.environ.pop(k, None)
            logger.debug(f"Environment variable unloaded: popped {k}")
        else:
            os.environ[k] = original_val
            logger.debug(f"Environment variable unloaded: restored {k}={original_val}")


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
