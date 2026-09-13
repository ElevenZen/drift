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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Mapping

from .constants import INTERNAL_RENDER_COMMAND, FORBIDDEN_RENDER_ENGINE_SUFFIXES
from .exceptions import ConfigError

logger = logging.getLogger(__name__)


@dataclass
class RenderEngineConfig:
    """Represents a render engine configuration inside workspace configuration."""
    name: str
    input_file: Path = Path("")
    suffix: str = ""
    render_command: str = ""

    def __post_init__(self) -> None:
        """Coerces any string path fields to pathlib.Path objects for absolute safety."""
        self.input_file = Path(self.input_file) if self.input_file is not None else Path("")

    @property
    def is_internal(self) -> bool:
        """Returns True if the engine uses Drift's built-in Python substitution renderer."""
        return self.render_command == INTERNAL_RENDER_COMMAND

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
        if not self.render_command or not isinstance(self.render_command, str):
            raise ConfigError("render_command must be a non-empty string.")
        if not self.is_internal:
            if not isinstance(self.input_file, Path) or str(self.input_file) in ("", "."):
                raise ConfigError("input_file must be a non-empty Path.")

    @property
    def is_disabled(self) -> bool:
        """Returns True if the render engine is disabled due to missing or empty input file."""
        if self.is_internal:
            return False
        return not self.input_file or str(self.input_file) in ("", ".")

    def copy(self) -> "RenderEngineConfig":
        """Returns a copy of the RenderEngineConfig."""
        return replace(self)

    def patch(self, override: Union["RenderEngineConfig", Mapping[str, Any]]) -> "RenderEngineConfig":
        """Creates a new RenderEngineConfig by applying non-empty override fields onto this config."""
        if isinstance(override, Mapping):
            override = RenderEngineConfig(
                name=self.name,
                input_file=Path(override.get("input_file", "")),
                suffix=str(override.get("suffix", "")),
                render_command=str(override.get("render_command", "")),
            )
        elif not isinstance(override, RenderEngineConfig):
            raise ConfigError(f"Cannot patch render engine '{self.name}' with {type(override).__name__}")

        return replace(
            self,
            input_file=override.input_file if str(override.input_file) not in ("", ".") else self.input_file,
            suffix=override.suffix if override.suffix else self.suffix,
            render_command=override.render_command if override.render_command else self.render_command,
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


class RenderEngineRegistry(MutableMapping[str, RenderEngineConfig]):
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
        if engines and not isinstance(engines, (RenderEngineRegistry, dict, Mapping)):
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
        if not overrides:
            return self.copy()

        override_items = dict(overrides)
        merged: Dict[str, RenderEngineConfig] = {
            name: engine.patch(override_items[name]) if name in override_items else engine.copy()
            for name, engine in self._engines.items()
        }

        # Add new engines from overrides that are not in self
        new_engines: Dict[str, RenderEngineConfig] = {
            name: (
                override.copy()
                if isinstance(override, RenderEngineConfig)
                else RenderEngineConfig(
                    name=name,
                    input_file=Path(override.get("input_file", "")),
                    suffix=str(override.get("suffix", "")),
                    render_command=str(override.get("render_command", "")),
                )
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
    def from_dict(cls, render_data: Any, base_dir: Path) -> "RenderEngineRegistry":
        """Builds a RenderEngineRegistry collection from parsed [render.*] TOML dictionary,
        resolving any relative input_file to base_dir.
        """
        if not render_data:
            return cls()
        if not isinstance(render_data, dict):
            raise ConfigError("'[render]' must be a TOML table.")
        if not isinstance(base_dir, Path):
            base_dir = Path(base_dir)

        known_render_keys = {"input_file", "suffix", "render_command"}

        def build_engine(name: str, config_dict: Any) -> RenderEngineConfig:
            if not isinstance(config_dict, dict):
                raise ConfigError(f"Render engine '{name}' configuration must be a dictionary.")
            for key in config_dict:
                if key not in known_render_keys:
                    raise ConfigError(f"Unknown option under render.{name}: '{key}'")
            raw_input = Path(config_dict.get("input_file", ""))
            input_path = (base_dir / raw_input) if (str(raw_input) not in ("", ".") and not raw_input.is_absolute()) else raw_input
            return RenderEngineConfig(
                name=name,
                input_file=input_path,
                suffix=str(config_dict.get("suffix", "")),
                render_command=str(config_dict.get("render_command", ""))
            )

        configs = {
            name: build_engine(name, config_dict)
            for name, config_dict in render_data.items()
            if isinstance(config_dict, dict)
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
        target_names: List[str]
    ) -> Optional[RenderSourceMatch]:
        """
        Locates a file or directory in the given directory that will render to one of the rendered names.
        Checks for static files/dirs first, then for templates using defined render engines.
        Returns a RenderSourceMatch or None if no match is found.

        The callers include package_config_render, drift_new, reverse_sync.
        """
        # 1. Static check
        for name in target_names:
            p = directory / name
            if p.exists():
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
        or status="block" if an intermediate path segment is blocked by a file.
        """
        from .file_utils import translate_dot_prefixes_reverse
        
        translated_path = translate_dot_prefixes_reverse(rel_target_path)
        parts = translated_path.parts
        
        current_dir = src_pkg_dir
        for i, part in enumerate(parts):
            match = self.find_source_file_for_rendered_names(current_dir, [part])
            
            if match:
                if i == len(parts) - 1:
                    # Last segment reached: exact conflict (match).
                    match.status = "match"
                    return match
                else:
                    # Not last segment: if it's a file, it's a conflict (file blocking directory).
                    if match.path.is_file():
                        match.status = "block"
                        return match
                    # Directory found, descend for next segment.
                    current_dir = match.path
            else:
                # No match for this segment, no conflict possible for this path.
                return None
                
            if not current_dir.exists() or not current_dir.is_dir():
                return None
        return None
