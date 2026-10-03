"""Process-level static lifetime cache storing NodeHashes for rendered files.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: Process-Level In-Memory Cache Primitives
    - NodeHashes: Immutable container holding own_hash and merkle_hash
    - CachedFileEntry: Container holding NodeHashes and source file stat fingerprint
    - RenderCache: Encapsulated cache mapping dst_path -> CachedFileEntry
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


@dataclass(frozen=True)
class CachedFileEntry:
    """Cache entry recording node hashes and source file stat fingerprint."""

    hashes: NodeHashes
    source_mtime_ns: Optional[int] = None
    source_size: Optional[int] = None


class RenderCache:
    """Thread-safe cache storing dst_path -> CachedFileEntry.

    Persists across package renders during a render session, allowing downstream
    dependents or later packages to reuse digested files without re-reading disk
    or re-computing hashes. Supports source file stat fingerprinting (mtime_ns + size)
    to automatically invalidate entries if the underlying source file is modified.
    """

    def __init__(self) -> None:
        self._cache: Dict[Path, CachedFileEntry] = {}
        self.existence_check_enabled: bool = True  # Optional runtime toggle for testing or performance

    def set_existence_check_enabled(self, enabled: bool) -> None:
        self.existence_check_enabled = enabled

    def check_entry_valid(
        self,
        dst_path: Path,
        src_path: Optional[Path],
        entry: CachedFileEntry,
    ) -> bool:
        """Validates that dst_path exists and src_path stat matches fingerprint."""
        if not self.existence_check_enabled:
            return True
        if not dst_path.exists():
            return False
        if src_path is not None:
            if not src_path.exists():
                return False
            if entry.source_mtime_ns is not None and entry.source_size is not None:
                try:
                    st = src_path.stat()
                    if st.st_mtime_ns != entry.source_mtime_ns or st.st_size != entry.source_size:
                        return False
                except OSError:
                    return False
        return True

    def get(self, dst_path: Path, src_path: Optional[Path] = None) -> Optional[NodeHashes]:
        """Retrieves NodeHashes for a given destination path if cached and valid."""
        entry = self._cache.get(dst_path)
        if entry is None:
            return None
        if not self.check_entry_valid(dst_path, src_path, entry):
            self._cache.pop(dst_path, None)
            return None
        return entry.hashes

    def set(
        self,
        dst_path: Path,
        hashes: NodeHashes,
        src_path: Optional[Path] = None,
    ) -> None:
        """Stores NodeHashes for a given destination path, recording source stat fingerprint if available."""
        mtime_ns: Optional[int] = None
        size: Optional[int] = None
        if src_path is not None and src_path.exists():
            try:
                st = src_path.stat()
                mtime_ns = st.st_mtime_ns
                size = st.st_size
            except OSError:
                pass
        self._cache[dst_path] = CachedFileEntry(
            hashes=hashes,
            source_mtime_ns=mtime_ns,
            source_size=size,
        )

    def register(
        self,
        dst_path: Path,
        own_hash: str,
        merkle_hash: str,
        src_path: Optional[Path] = None,
    ) -> None:
        """Convenience method registering own_hash and merkle_hash as NodeHashes."""
        self.set(
            dst_path,
            NodeHashes(own_hash=own_hash, merkle_hash=merkle_hash),
            src_path=src_path,
        )

    def contains(self, dst_path: Path, src_path: Optional[Path] = None) -> bool:
        """Checks if a destination path has already been cached and remains valid in this run."""
        return self.get(dst_path, src_path=src_path) is not None

    def clear(self) -> None:
        """Clears all cached entries. Used in tests or fresh runs."""
        self._cache.clear()

    def invalidate_prefix(self, prefix: Path) -> None:
        """Removes all cached entries whose paths start with or match prefix."""
        from ..utils.path_utils import is_relative_to
        to_del = [p for p in self._cache if is_relative_to(p, prefix)]
        for p in to_del:
            self._cache.pop(p, None)

    def items(self) -> ItemsView[Path, NodeHashes]:
        """Returns view of all cached (path, hashes) pairs."""
        return {p: entry.hashes for p, entry in self._cache.items()}.items()

    def __contains__(self, dst_path: Path) -> bool:
        return self.contains(dst_path)

    def __len__(self) -> int:
        return len(self._cache)

    def __bool__(self) -> bool:
        return True
