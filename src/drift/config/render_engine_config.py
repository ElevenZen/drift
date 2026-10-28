"""Render engine configuration model, registry collection, and source matching.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: First-Class Collection Registry
    RenderEngineRegistry (MutableMapping[str, RenderEngineConfig])
        - overlay(overrides): Field-level inheritance and engine patching
        - from_dict(render_data): Declarative dictionary constructor
        - find_engine_for_file(filename): Suffix-matching engine locator
        - make_new_template_name(old, new): Template naming for reverse-sync / add
        - find_source_file_for_rendered_names(dir, names): Multi-engine source match
        - find_conflict_in_source_dir(dir, rel_path): Path conflict & blocking detector

Layer 1: Individual Engine Model & Data Structures
    RenderEngineConfig (Dataclass)
        - patch(override): Pure field-level patching helper
        - validate(): Strict validation of engine fields
        - strip_suffix(filename): Removes engine suffix from filename
    RenderSourceMatch (Dataclass)
        - Encapsulates matched source file, engine, and status (match / block)
===============================================================================
"""

import logging
from collections.abc import MutableMapping, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, Tuple, Any, Union, Sequence, cast

from ..core.constants import INTERNAL_RENDER_COMMAND, FORBIDDEN_RENDER_ENGINE_SUFFIXES
from ..core.exceptions import ConfigError
from ..core.mixins import AlwaysTruthy
from ..utils.func_utils import (
    branch,
    choose,
    when_none,
    when_not_none,
)
from ..utils.config_utils import (
    parse_command_tokens,
    parse_optional_config_path,
    validate_known_keys,
)
from ..utils.path_utils import check_is_partially_anchored_path

logger = logging.getLogger(__name__)


def parse_render_command(raw_cmd: Any, engine_name: str = "") -> List[str]:
    """Parses a raw render_command value (string or sequence of strings) into a List[str]."""
    ctx = choose(bool(engine_name), f"for render engine '{engine_name}'", "")
    return parse_command_tokens(raw_cmd, field_name="render_command", context=ctx)


def parse_render_input_file(
    raw_input: Any,
    base_dir: Path,
    engine_name: str = "",
) -> Optional[Path]:
    """Parses, resolves, and validates a render engine's input_file against base_dir."""
    ctx = choose(bool(engine_name), f"for render engine '{engine_name}'", "")
    return parse_optional_config_path(
        raw_input,
        field_name="input_file",
        dot_as_none=False,
        base_dir=base_dir,
        must_be_inside_base=True,
        context=ctx,
    )


@dataclass
class RenderEngineConfig(AlwaysTruthy):
    """Represents a render engine configuration inside workspace configuration."""
    KNOWN_KEYS: ClassVar[Tuple[str, ...]] = ("input_file", "suffix", "render_command")

    name: str
    input_file: Optional[Path] = None
    suffix: str = ""
    render_command: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Normalizes input_file into a non-empty Path object, or None if empty/dot."""
        self.input_file = parse_optional_config_path(
            self.input_file,
            field_name="input_file",
            dot_as_none=True,
        )

    @property
    def is_internal(self) -> bool:
        """Returns True if the engine uses Drift's built-in Python substitution renderer."""
        return self.render_command == [INTERNAL_RENDER_COMMAND]

    @property
    def requires_input_file(self) -> bool:
        """Returns True if the render command references the '%i' input file placeholder."""
        if self.is_internal:
            return False
        return any("%i" in token for token in self.render_command)

    def validate(self) -> None:
        """Validates render engine configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ConfigError("Render engine must have a non-empty 'name'.")
        if not self.suffix or not isinstance(self.suffix, str):
            raise ConfigError("suffix must be a non-empty string.")
        if "." in self.suffix:
            raise ConfigError(f"Render engine suffix '{self.suffix}' cannot contain dots ('.').")
        if self.suffix.lower() in FORBIDDEN_RENDER_ENGINE_SUFFIXES:
            raise ConfigError(f"Render engine suffix '{self.suffix}' is a forbidden reserved keyword.")
        if (
            not self.render_command
            or not isinstance(self.render_command, list)
            or not all(isinstance(token, str) and bool(token) for token in self.render_command)
        ):
            raise ConfigError("render_command must be a non-empty list of strings.")
        if self.requires_input_file and self.input_file is None:
            raise ConfigError("input_file must be specified for render engine when '%i' is used in render_command.")
        if self.input_file is not None and check_is_partially_anchored_path(self.input_file):
            raise ConfigError(
                f"Render engine '{self.name}' input_file '{self.input_file}' must be a relative or valid absolute path."
            )

    @classmethod
    def from_dict(cls, name: str, config_dict: Any, base_dir: Path) -> "RenderEngineConfig":
        """Builds a RenderEngineConfig instance from a parsed [render.<name>] TOML dictionary."""
        if not isinstance(config_dict, dict):
            raise ConfigError(f"Render engine '{name}' configuration must be a dictionary.")
        validate_known_keys(
            config_dict,
            cls.KNOWN_KEYS,
            context=f"render.{name}",
        )
        return cls(
            name=name,
            input_file=parse_render_input_file(
                config_dict.get("input_file"),
                base_dir=base_dir,
                engine_name=name,
            ),
            suffix=str(config_dict.get("suffix", "")),
            render_command=parse_render_command(config_dict.get("render_command"), name),
        )

    def copy(self) -> "RenderEngineConfig":
        """Returns a copy of the RenderEngineConfig."""
        return replace(self, render_command=list(self.render_command))

    def patch(self, override: Union["RenderEngineConfig", Mapping[str, Any]]) -> "RenderEngineConfig":
        """Creates a new RenderEngineConfig by applying non-empty override fields onto this config."""
        if isinstance(override, Mapping):
            override = RenderEngineConfig(
                name=self.name,
                input_file=override.get("input_file"),
                suffix=str(override.get("suffix", "")),
                render_command=parse_render_command(override.get("render_command"), self.name),
            )
        elif not isinstance(override, RenderEngineConfig):
            raise ConfigError(f"Cannot patch render engine '{self.name}' with {type(override).__name__}")

        return replace(
            self,
            input_file=when_none(override.input_file, self.input_file),
            suffix=choose(bool(override.suffix), override.suffix, self.suffix),
            render_command=branch(
                bool(override.render_command),
                lambda: list(override.render_command),
                lambda: list(self.render_command),
            ),
        )

    def strip_suffix(self, filename: str) -> str:
        """Strips the engine suffix segment from the filename, replacing only the last occurrence."""
        suffix = self.suffix
        if filename.endswith(f".{suffix}"):
            return filename[:-len(f".{suffix}")]
        
        pattern = f".{suffix}."
        idx = filename.rfind(pattern)
        if idx != -1:
            # Replaces only the last occurrence of the pattern with "."
            return filename[:idx] + "." + filename[idx + len(pattern):]
        return filename


@dataclass
class RenderSourceMatch:
    """Encapsulates a match or blocking entry found in the source directory."""
    path: Path
    engine: Optional[RenderEngineConfig]
    target_name: str
    status: str = "match"  # "match" or "block"

    @property
    def is_templated(self) -> bool:
        """Returns True if the matched source file is rendered by a template engine."""
        return self.engine is not None


class RenderEngineRegistry(AlwaysTruthy, MutableMapping[str, RenderEngineConfig]):
    """First-class collection registry managing multiple named RenderEngineConfig objects.

    Uses composition (has-a Dict[str, RenderEngineConfig]) and implements MutableMapping
    to provide full dictionary ergonomics with strict validation, template resolution,
    and conflict detection methods.
    """

    def __init__(
        self,
        engines: Mapping[str, RenderEngineConfig] = {},
        **kwargs: RenderEngineConfig
    ) -> None:
        if engines is not None and not isinstance(engines, (RenderEngineRegistry, dict, Mapping)):
            raise ConfigError(f"engines must be a Mapping, got {type(engines).__name__}")
        self._engines: Dict[str, RenderEngineConfig] = {}
        if engines:
            if isinstance(engines, RenderEngineRegistry):
                self._engines.update(engines._engines)
            else:
                for k, v in engines.items():
                    self[k] = v
        if kwargs:
            for k, v in kwargs.items():
                self[k] = v

    def __getitem__(self, key: str) -> RenderEngineConfig:
        return self._engines[key]

    def __setitem__(self, key: str, value: RenderEngineConfig) -> None:
        if not isinstance(value, RenderEngineConfig):
            raise ConfigError(
                f"render engine value for '{key}' must be a RenderEngineConfig instance, got {type(value).__name__}."
            )
        self._engines[key] = value

    def __delitem__(self, key: str) -> None:
        del self._engines[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._engines)

    def __len__(self) -> int:
        return len(self._engines)

    def __contains__(self, key: object) -> bool:
        return key in self._engines

    def __repr__(self) -> str:
        return f"RenderEngineRegistry({self._engines!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RenderEngineRegistry):
            return self._engines == other._engines
        if isinstance(other, dict):
            return self._engines == other
        return False

    def copy(self) -> "RenderEngineRegistry":
        return RenderEngineRegistry(self._engines.copy())

    def overlay(
        self,
        overrides: Optional[Union["RenderEngineRegistry", Mapping[str, Any]]] = None
    ) -> "RenderEngineRegistry":
        """Creates a new RenderEngineRegistry by overlaying package-level overrides onto this registry.

        Implements field-level inheritance/patching:
        - Inherits base engine fields from self, overriding only specified non-empty fields from overrides.
        - Adds new engine definitions declared in overrides.
        - Validates the resulting merged registry.
        """
        if not overrides or len(overrides) == 0:
            return self.copy()

        override_items = dict(overrides)
        merged: Dict[str, RenderEngineConfig] = {
            name: branch(
                name in override_items,
                lambda: engine.patch(override_items[name]),
                lambda: engine.copy(),
            )
            for name, engine in self._engines.items()
        }

        # Add new engines from overrides that are not in self
        new_engines: Dict[str, RenderEngineConfig] = {
            name: branch(
                isinstance(override, RenderEngineConfig),
                lambda: cast(RenderEngineConfig, override).copy(),
                lambda: RenderEngineConfig(
                    name=name,
                    input_file=cast(Mapping[str, Any], override).get("input_file"),
                    suffix=str(cast(Mapping[str, Any], override).get("suffix", "")),
                    render_command=parse_render_command(
                        cast(Mapping[str, Any], override).get("render_command"),
                        name,
                    ),
                ),
            )
            for name, override in override_items.items()
            if name not in self._engines
        }
        merged.update(new_engines)

        result = RenderEngineRegistry(merged)
        result.validate()
        return result

    def validate(self) -> None:
        """Validates all contained RenderEngineConfig instances."""
        for name, engine in self._engines.items():
            if not isinstance(engine, RenderEngineConfig):
                raise ConfigError(f"Render engine value for '{name}' must be a RenderEngineConfig instance.")
            engine.validate()

    @classmethod
    def from_dict(cls, config_dict: Any, base_dir: Path) -> "RenderEngineRegistry":
        """Builds a RenderEngineRegistry collection from parsed [render.*] TOML dictionary,
        resolving any relative input_file to base_dir.
        """
        if not config_dict:
            return cls()
        if not isinstance(config_dict, dict):
            raise ConfigError("'[render]' must be a TOML table.")
        base = branch(isinstance(base_dir, Path), lambda: base_dir, lambda: Path(base_dir))

        configs = {
            name: RenderEngineConfig.from_dict(name, engine_dict, base_dir=base)
            for name, engine_dict in config_dict.items()
            if isinstance(engine_dict, dict)
        }
        return cls(configs)

    def find_engine_for_file(self, filename: str) -> Optional[RenderEngineConfig]:
        """Finds which engine (if any) should render the given file based on suffix patterns."""
        for engine in self._engines.values():
            suffix = engine.suffix
            if not suffix:
                continue
            if filename.endswith(f".{suffix}"):
                return engine
            if f".{suffix}." in filename:
                return engine
        return None

    def make_new_template_name(self, old_template_name: str, new_rendered_name: str) -> str:
        """Calculates the new template filename based on the old template's engine suffix and the new target filename.
        
        Example: old_template_name = "dot-old.envst.sh", new_rendered_name = "dot-new.sh"
                 Returns: "dot-new.envst.sh"
                 old_template_name = "dot-old.envst", new_rendered_name = "dot-new"
                 Returns: "dot-new.envst"
        """
        old_parts = old_template_name.split(".")
        
        # Determine engine config suffixes dynamically from workspace configurations
        valid_suffixes = {engine.suffix for engine in self._engines.values() if engine.suffix}
        
        # Search the old parts for any valid engine suffix
        engine_suffix = next((part for part in reversed(old_parts) if part in valid_suffixes), None)
        if not engine_suffix:
            return new_rendered_name  # No engine suffix found, return the new name as is

        dot_idx = new_rendered_name.rfind('.')
        if dot_idx == -1:
            return f"{new_rendered_name}.{engine_suffix}"
        else:
            return new_rendered_name[:dot_idx] + f".{engine_suffix}" + new_rendered_name[dot_idx:]

    def find_source_file_for_rendered_names(
        self, 
        directory: Path, 
        target_names: Sequence[str]
    ) -> Optional[RenderSourceMatch]:
        """
        Locates a source file in the given directory that will render to one of the rendered names.
        Checks for static files first, then for templates using defined render engines.
        Returns a RenderSourceMatch or None if no match is found.

        The callers include package_config_render, drift_new, reverse_sync.
        """
        # 1. Static check (files only, never directories)
        for name in target_names:
            p = directory / name
            if p.is_file():
                return RenderSourceMatch(path=p, engine=None, target_name=name, status="match")

        # 2. Template check (using defined engines)
        # Only normal file templates are considered for rendering; directories are not rendered.
        # So directories with a template suffix won't conflict.
        for engine in self._engines.values():
            suffix = engine.suffix
            if not suffix:
                continue
            for name in target_names:
                # Check for template form 1: name.suffix (e.g., config.envst)
                template_name_1 = f"{name}.{suffix}"
                p1 = directory / template_name_1
                if p1.is_file():
                    return RenderSourceMatch(path=p1, engine=engine, target_name=name, status="match")

                # Check for template form 2: name with suffix inserted before last dot (e.g., config.envst.toml)
                dot_idx = name.rfind('.')
                if dot_idx != -1:
                    template_name_2 = name[:dot_idx] + f".{suffix}" + name[dot_idx:]
                    p2 = directory / template_name_2
                    if p2.is_file():
                        return RenderSourceMatch(path=p2, engine=engine, target_name=name, status="match")
        return None

    def find_conflict_in_source_dir(
        self,
        src_pkg_dir: Path,
        rel_target_path: Path
    ) -> Optional[RenderSourceMatch]:
        """
        Finds a source file that renders to rel_target_path or a blocking path.
        Returns RenderSourceMatch with status="match" if it's an exact rendering match,
        or status="block" if an intermediate path segment is blocked by a file or if
        the leaf target file is blocked by an existing directory.
        """
        from ..utils.path_utils import decode_dot_prefix

        translated_path = decode_dot_prefix(rel_target_path)
        parts = translated_path.parts

        current_dir = src_pkg_dir
        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                # Intermediate segment: check if blocked by template/file
                render_match = self.find_source_file_for_rendered_names(current_dir, [part])
                if render_match and render_match.path.is_file():
                    render_match.status = "block"
                    return render_match

                # Descend concrete directory
                next_dir = current_dir / part
                if not next_dir.exists() and not next_dir.is_symlink():
                    return None
                if not next_dir.is_dir():
                    return RenderSourceMatch(path=next_dir, engine=None, target_name=part, status="block")
                current_dir = next_dir
            else:
                leaf_path = current_dir / part
                # Check if leaf target file is blocked by an existing folder in source
                if leaf_path.is_dir():
                    return RenderSourceMatch(path=leaf_path, engine=None, target_name=part, status="block")

                # Leaf segment: exact match check for file/template
                render_match = self.find_source_file_for_rendered_names(current_dir, [part])
                if render_match:
                    render_match.status = "match"
                    return render_match

                # Broken symlink blocking check
                if leaf_path.is_symlink() and not leaf_path.exists():
                    return RenderSourceMatch(path=leaf_path, engine=None, target_name=part, status="block")

                return None
        return None
