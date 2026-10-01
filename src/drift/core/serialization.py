"""Core JSON serialization helpers and base model."""

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict


def serialize_for_json(obj: Any) -> Any:
    """Recursively converts Dataclasses, Paths, Enums, and Sets into standard JSON primitives."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if hasattr(obj, "to_dict") and callable(obj.to_dict) and not isinstance(obj, type):
        return obj.to_dict()
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple, set)):
        return [serialize_for_json(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): serialize_for_json(v) for k, v in obj.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: serialize_for_json(getattr(obj, f.name)) for f in fields(obj)}
    return str(obj)


@dataclass
class SerializableModel:
    """Base dataclass providing automatic dictionary and JSON serialization."""

    def to_dict(self) -> Dict[str, Any]:
        """Converts model to a JSON-serializable dictionary."""
        return {f.name: serialize_for_json(getattr(self, f.name)) for f in fields(self)}

    def to_json(self, indent: int = 2) -> str:
        """Serializes model to a formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)
