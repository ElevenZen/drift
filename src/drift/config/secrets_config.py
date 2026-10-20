"""Workspace secrets configuration model and decryption provider execution.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Workspace Secrets Provider Execution
    fetch_workspace_secrets(drift_root, secrets_config=None)
        - Resolves plaintext fallback (`config/secrets.env`), explicit plaintext `secrets_file`,
          or in-memory subprocess decryption (`decrypt_command` with `%i` and `%s`).

Layer 1: [secrets] Section Model & Field Parsers
    SecretsConfig (Dataclass)
        - decrypt_command, secrets_file, key_file
        - validate(): Placeholder (`%i`, `%s`) and 3-state hybrid path validation
        - from_dict(data): Declarative dictionary constructor
    parse_decrypt_command(raw_cmd)
===============================================================================
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from ..core.constants import CONFIG_DIR_NAME, SECRETS_ENV_FILE_NAME
from ..core.exceptions import ConfigError
from ..core.mixins import AlwaysTruthy
from ..utils.config_utils import (
    branch,
    parse_command_tokens,
    parse_optional_config_path,
    validate_known_keys,
    when_none,
    when_not_none,
)
from ..utils.env_utils import parse_env_text, read_plain_secrets_file
from ..utils.path_utils import check_is_partially_anchored_path
from ..utils.process_utils import run_command


def parse_decrypt_command(raw_cmd: Any) -> List[str]:
    """Parses a raw decrypt_command value (string or sequence of strings) into a List[str]."""
    return parse_command_tokens(raw_cmd, field_name="decrypt_command", context="under [secrets]")


@dataclass
class SecretsConfig(AlwaysTruthy):
    """Represents options defined under the [secrets] section in drift_workspace.toml."""
    KNOWN_KEYS: ClassVar[Tuple[str, ...]] = (
        "decrypt_command",
        "secrets_file",
        "key_file",
    )

    decrypt_command: List[str] = field(default_factory=list)
    secrets_file: Optional[Path] = None
    key_file: Optional[Path] = None

    def __init__(
        self,
        decrypt_command: Union[str, Sequence[str], None] = None,
        secrets_file: Optional[Union[Path, str]] = None,
        key_file: Optional[Union[Path, str]] = None,
    ) -> None:
        self.decrypt_command = parse_decrypt_command(decrypt_command)
        self.secrets_file = parse_optional_config_path(
            secrets_file,
            field_name="secrets_file",
            expand=True,
            dot_as_none=True,
            context="under [secrets]",
        )
        self.key_file = parse_optional_config_path(
            key_file,
            field_name="key_file",
            expand=True,
            dot_as_none=True,
            context="under [secrets]",
        )

    @property
    def requires_secrets_file(self) -> bool:
        """Returns True if decrypt_command references the '%s' secrets file placeholder."""
        return any("%s" in token for token in self.decrypt_command)

    @property
    def requires_key_file(self) -> bool:
        """Returns True if decrypt_command references the '%i' key file placeholder."""
        return any("%i" in token for token in self.decrypt_command)

    def resolve_secrets_file(self, drift_root: Path) -> Optional[Path]:
        """Resolves secrets_file against drift_root/config if relative, or returns it directly if absolute."""
        base_dir = Path(drift_root).resolve() / CONFIG_DIR_NAME
        return when_not_none(
            self.secrets_file,
            lambda p: branch(bool(p.anchor), lambda: p, lambda: base_dir / p),
        )

    def resolve_key_file(self, drift_root: Path) -> Optional[Path]:
        """Resolves key_file against drift_root/config if relative, or returns it directly if absolute."""
        base_dir = Path(drift_root).resolve() / CONFIG_DIR_NAME
        return when_not_none(
            self.key_file,
            lambda p: branch(bool(p.anchor), lambda: p, lambda: base_dir / p),
        )

    def validate(self) -> None:
        """Validates [secrets] section configuration values."""
        if not isinstance(self.decrypt_command, list) or not all(
            isinstance(token, str) and bool(token.strip()) for token in self.decrypt_command
        ):
            raise ConfigError("decrypt_command under [secrets] must be a list of non-empty strings.")
        if self.secrets_file is not None:
            if not isinstance(self.secrets_file, Path):
                raise ConfigError(
                    f"secrets_file under [secrets] must be a Path, got {type(self.secrets_file).__name__}."
                )
            if check_is_partially_anchored_path(self.secrets_file):
                raise ConfigError(
                    f"secrets_file '{self.secrets_file}' under [secrets] must be a relative or valid absolute path."
                )
        if self.key_file is not None:
            if not isinstance(self.key_file, Path):
                raise ConfigError(
                    f"key_file under [secrets] must be a Path, got {type(self.key_file).__name__}."
                )
            if check_is_partially_anchored_path(self.key_file):
                raise ConfigError(
                    f"key_file '{self.key_file}' under [secrets] must be a relative or valid absolute path."
                )
        if self.requires_secrets_file and self.secrets_file is None:
            raise ConfigError("secrets_file must be specified under [secrets] when '%s' is used in decrypt_command.")
        if self.requires_key_file and self.key_file is None:
            raise ConfigError("key_file must be specified under [secrets] when '%i' is used in decrypt_command.")

    @classmethod
    def from_dict(cls, data: Any) -> "SecretsConfig":
        """Builds a SecretsConfig instance from a parsed TOML dictionary."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("[secrets] must be a TOML table.")
        if not data:
            return cls()
        validate_known_keys(data, cls.KNOWN_KEYS, context="[secrets]")
        cfg = cls(
            decrypt_command=data.get("decrypt_command"),
            secrets_file=data.get("secrets_file"),
            key_file=data.get("key_file"),
        )
        cfg.validate()
        return cfg


def fetch_workspace_secrets(
    drift_root: Path,
    secrets_config: Optional[SecretsConfig] = None,
) -> Dict[str, str]:
    """Fetches and parses workspace secrets according to [secrets] configuration.

    1. When `secrets_config` is None or defines neither `decrypt_command` nor `secrets_file`,
       reads `config/secrets.env` as an optional plaintext `.env` file (`required=False`).
    2. When `secrets_file` is specified without `decrypt_command`, reads `secrets_file`
       strictly as UTF-8 `.env` text (`required=True`), raising ConfigError if missing,
       binary/non-UTF-8, or malformed.
    3. When `decrypt_command` is specified, validates any configured `secrets_file` and
       `key_file`, substitutes `%i` (key_file) and `%s` (secrets_file), executes the
       command in-memory, and strictly parses stdout as `.env` key-value pairs.
    """
    if secrets_config is not None and not isinstance(secrets_config, SecretsConfig):
        raise ConfigError(
            f"secrets_config must be a SecretsConfig instance or None, got {type(secrets_config).__name__}."
        )
    root = Path(drift_root).resolve()
    cfg = when_none(secrets_config, SecretsConfig)
    cfg.validate()

    if not cfg.decrypt_command:
        return branch(
            cfg.secrets_file is None,
            lambda: read_plain_secrets_file(root / CONFIG_DIR_NAME / SECRETS_ENV_FILE_NAME, required=False),
            lambda: read_plain_secrets_file(cast(Path, cfg.resolve_secrets_file(root)), required=True),
        )

    secrets_path = cfg.resolve_secrets_file(root)
    if secrets_path is not None and not secrets_path.is_file():
        raise ConfigError(f"Configured secrets_file '{secrets_path}' under [secrets] does not exist.")

    key_path = cfg.resolve_key_file(root)
    if key_path is not None and not key_path.is_file():
        raise ConfigError(f"Configured key_file '{key_path}' under [secrets] does not exist.")

    def _substitute_token(token: str) -> str:
        with_key = branch(
            key_path is not None and "%i" in token,
            lambda: token.replace("%i", cast(Path, key_path).as_posix()),
            lambda: token,
        )
        return branch(
            secrets_path is not None and "%s" in with_key,
            lambda: with_key.replace("%s", cast(Path, secrets_path).as_posix()),
            lambda: with_key,
        )

    cmd = [_substitute_token(tok) for tok in cfg.decrypt_command]
    config_dir = root / CONFIG_DIR_NAME
    cwd = branch(config_dir.is_dir(), lambda: config_dir, lambda: root)
    try:
        res = run_command(cmd, cwd=cwd, text=True, suppress_output=True)
    except subprocess.CalledProcessError as exc:
        stderr_msg = (exc.stderr or "").strip()
        detail = branch(bool(stderr_msg), lambda: f": {stderr_msg}", lambda: f" (exit code {exc.returncode})")
        raise ConfigError(f"Failed to execute decrypt_command under [secrets]{detail}") from exc
    except (FileNotFoundError, OSError) as exc:
        raise ConfigError(f"Failed to execute decrypt_command under [secrets]: {exc}") from exc

    return parse_env_text(res.stdout or "", strict=True, source_desc="secrets decrypt_command")
