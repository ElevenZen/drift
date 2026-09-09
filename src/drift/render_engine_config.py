"""Render engine configuration model, registry collection, and source matching."""

import logging
from collections.abc import MutableMapping, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any, Union

from .constants import INTERNAL_RENDER_COMMAND
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
            raise ValueError("Render engine must have a non-empty 'name'.")
        if not self.suffix or not isinstance(self.suffix, str):
            raise ValueError("suffix must be a non-empty string.")
        if "." in self.suffix:
            raise ValueError(f"Render engine suffix '{self.suffix}' cannot contain dots ('.').")
        if not self.render_command or not isinstance(self.render_command, str):
            raise ValueError("render_command must be a non-empty string.")
        if not self.is_internal:
            if not isinstance(self.input_file, Path) or str(self.input_file) in ("", "."):
                raise ValueError("input_file must be a non-empty Path.")

    @property
    def is_disabled(self) -> bool:
        """Returns True if the render engine is disabled due to missing or empty input file."""
        if self.is_internal:
            return False
        return not self.input_file or str(self.input_file) in ("", ".")

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


class RenderEngineRegistry(MutableMapping):
    """Registry and query manager for workspace render engines.

    Uses composition (has-a Dict[str, RenderEngineConfig]) and implements MutableMapping
    to provide full dictionary ergonomics with strict validation, template resolution,
    and conflict detection methods.
    """

    def __init__(
        self,
        engines: Optional[Union[Dict[str, RenderEngineConfig], "RenderEngineRegistry"]] = None,
        **kwargs: RenderEngineConfig
    ) -> None:
        self._engines: Dict[str, RenderEngineConfig] = {}
        if engines is not None:
            if isinstance(engines, RenderEngineRegistry):
                self._engines.update(engines._engines)
            elif isinstance(engines, dict):
                for k, v in engines.items():
                    self[k] = v
            else:
                for k, v in dict(engines).items():
                    self[k] = v
        if kwargs:
            for k, v in kwargs.items():
                self[k] = v

    def __getitem__(self, key: str) -> RenderEngineConfig:
        return self._engines[key]

    def __setitem__(self, key: str, value: RenderEngineConfig) -> None:
        if not isinstance(value, RenderEngineConfig):
            raise TypeError(
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

    def validate(self) -> None:
        """Validates all contained RenderEngineConfig instances."""
        for name, engine in self._engines.items():
            if not isinstance(engine, RenderEngineConfig):
                raise TypeError(f"Render engine value for '{name}' must be a RenderEngineConfig instance.")
            engine.validate()

    @classmethod
    def from_dict(cls, render_data: Any) -> "RenderEngineRegistry":
        """Builds a RenderEngineRegistry collection from parsed [render.*] TOML dictionary."""
        if not render_data:
            return cls()
        if not isinstance(render_data, dict):
            raise ConfigError("'[render]' must be a TOML table.")

        configs: Dict[str, RenderEngineConfig] = {}
        known_render_keys = {"input_file", "suffix", "render_command"}
        for name, config_dict in render_data.items():
            if isinstance(config_dict, dict):
                for key in config_dict:
                    if key not in known_render_keys:
                        raise ConfigError(f"Unknown option under render.{name}: '{key}'")
                configs[name] = RenderEngineConfig(
                    name=name,
                    input_file=Path(config_dict.get("input_file", "")),
                    suffix=str(config_dict.get("suffix", "")),
                    render_command=str(config_dict.get("render_command", ""))
                )
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
