"""Process-level static lifetime cache storing NodeHashes for rendered files.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: Process-Level In-Memory Cache Primitives
    - NodeHashes: Immutable container holding own_hash and merkle_hash
    - StaticRenderCache: Encapsulated cache mapping output_path -> NodeHashes
    - static_render_cache: Global public singleton instance shared across rendering modules
===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, ItemsView


@dataclass(frozen=True)
class NodeHashes:
    """Immutable pair containing both node own hash and Merkle hash."""

    own_hash: str
    merkle_hash: str


class StaticRenderCache:
    """Thread-safe and process-level cache storing output_path -> NodeHashes.

    Persists across package renders during a single `drift` command execution,
    allowing downstream dependents or later packages to reuse digested files
    without re-reading disk or re-computing hashes.
    """

    def __init__(self) -> None:
        self._cache: Dict[Path, NodeHashes] = {}

    def get(self, output_path: Path) -> Optional[NodeHashes]:
        """Retrieves NodeHashes for a given output path if cached."""
        return self._cache.get(output_path)

    def set(self, output_path: Path, hashes: NodeHashes) -> None:
        """Stores NodeHashes for a given output path."""
        self._cache[output_path] = hashes

    def register(self, output_path: Path, own_hash: str, merkle_hash: str) -> None:
        """Convenience method registering own_hash and merkle_hash as NodeHashes."""
        self._cache[output_path] = NodeHashes(own_hash=own_hash, merkle_hash=merkle_hash)

    def contains(self, output_path: Path) -> bool:
        """Checks if an output path has already been cached in this run."""
        return output_path in self._cache

    def clear(self) -> None:
        """Clears all cached entries. Used in tests or fresh runs."""
        self._cache.clear()

    def items(self) -> ItemsView[Path, NodeHashes]:
        """Returns view of all cached (path, hashes) pairs."""
        return self._cache.items()

    def __contains__(self, output_path: Path) -> bool:
        return self.contains(output_path)

    def __len__(self) -> int:
        return len(self._cache)

    def __bool__(self) -> bool:
        return True


# Public singleton instance shared across all render modules
static_render_cache = StaticRenderCache()
