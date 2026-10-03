"""Atomic 3-bucket lockfile persistence for package render caching.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: RenderLockfile Model & Atomic IO
    - RenderBucket enum (CONFIG, HOOKS, PAYLOAD)
    - RenderLockfile dataclass
        - config_hashes: Set[str]
        - hook_hashes: Set[str]
        - payload_hashes: Set[str]
        - load_from_dir(package_render_dir) -> RenderLockfile
        - save_to_dir(package_render_dir) -> None
        - get_bucket_hashes(bucket) -> Set[str]
        - update_bucket_hashes(bucket, hashes) -> RenderLockfile
        - update_config_hashes(hashes) -> RenderLockfile
        - update_hook_hashes(hashes) -> RenderLockfile
        - update_payload_hashes(hashes) -> RenderLockfile
        - check_lockfile_matches(bucket, node, drift_root) -> Optional[NodeHashes]
        - contains_hash(hash_str) -> bool
        - get_all_hashes() -> Set[str]
===============================================================================
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Set, Iterable, Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .render_cache import NodeHashes
    from .render_dag import Node

from ..core.constants import DRIFT_INTERNAL_DIR_NAME, RENDER_LOCK_FILE_NAME


class RenderBucket(str, Enum):
    """Enumeration of isolated lockfile hash buckets."""

    CONFIG = "config_hashes"
    HOOKS = "hook_hashes"
    PAYLOAD = "payload_hashes"


@dataclass
class RenderLockfile:
    """In-memory representation of render_lock.json with 3 distinct hash buckets."""

    config_hashes: Set[str] = field(default_factory=set)
    hook_hashes: Set[str] = field(default_factory=set)
    payload_hashes: Set[str] = field(default_factory=set)

    @classmethod
    def load_from_dir(cls, package_render_dir: Path) -> RenderLockfile:
        """Loads RenderLockfile from <package_render_dir>/.drift/render_lock.json.

        Gracefully handles missing file or malformed JSON by returning an empty RenderLockfile.
        """
        lock_path = package_render_dir / DRIFT_INTERNAL_DIR_NAME / RENDER_LOCK_FILE_NAME
        if not lock_path.is_file():
            return cls()

        try:
            raw_text = lock_path.read_text(encoding="utf-8")
            data: Dict[str, Any] = json.loads(raw_text)
            if not isinstance(data, dict):
                return cls()

            def extract_hashes(bucket_key: str) -> Set[str]:
                items = data.get(bucket_key)
                if not isinstance(items, list):
                    return set()
                return set(filter(lambda x: isinstance(x, str), items))

            return cls(
                config_hashes=extract_hashes("config_hashes"),
                hook_hashes=extract_hashes("hook_hashes"),
                payload_hashes=extract_hashes("payload_hashes"),
            )
        except (json.JSONDecodeError, OSError):
            return cls()

    def save_to_dir(self, package_render_dir: Path) -> None:
        """Atomically writes lockfile to <package_render_dir>/.drift/render_lock.json.

        Outputs formatted JSON with sorted hash lists to ensure clean, deterministic git diffs.
        """
        internal_dir = package_render_dir / DRIFT_INTERNAL_DIR_NAME
        internal_dir.mkdir(parents=True, exist_ok=True)

        lock_path = internal_dir / RENDER_LOCK_FILE_NAME
        temp_path = internal_dir / f".{RENDER_LOCK_FILE_NAME}.tmp.{os.getpid()}"

        payload = {
            "version": 1,
            "config_hashes": sorted(self.config_hashes),
            "hook_hashes": sorted(self.hook_hashes),
            "payload_hashes": sorted(self.payload_hashes),
        }

        json_text = json.dumps(payload, indent=2) + "\n"
        try:
            temp_path.write_text(json_text, encoding="utf-8")
            os.replace(temp_path, lock_path)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

    def get_bucket_hashes(self, bucket: RenderBucket) -> Set[str]:
        """Returns the set of hashes for the specified bucket."""
        if bucket == RenderBucket.CONFIG:
            return self.config_hashes
        elif bucket == RenderBucket.HOOKS:
            return self.hook_hashes
        elif bucket == RenderBucket.PAYLOAD:
            return self.payload_hashes
        raise ValueError(f"Unknown render bucket: {bucket}")

    def update_bucket_hashes(self, bucket: RenderBucket, hashes: Iterable[str]) -> RenderLockfile:
        """Replaces the specified bucket with the provided hashes."""
        if bucket == RenderBucket.CONFIG:
            return self.update_config_hashes(hashes)
        elif bucket == RenderBucket.HOOKS:
            return self.update_hook_hashes(hashes)
        elif bucket == RenderBucket.PAYLOAD:
            return self.update_payload_hashes(hashes)
        raise ValueError(f"Unknown render bucket: {bucket}")

    def update_config_hashes(self, hashes: Iterable[str]) -> RenderLockfile:
        """Replaces the config_hashes bucket with the provided hashes."""
        self.config_hashes = set(hashes)
        return self

    def update_hook_hashes(self, hashes: Iterable[str]) -> RenderLockfile:
        """Replaces the hook_hashes bucket with the provided hashes."""
        self.hook_hashes = set(hashes)
        return self

    def update_payload_hashes(self, hashes: Iterable[str]) -> RenderLockfile:
        """Replaces the payload_hashes bucket with the provided hashes."""
        self.payload_hashes = set(hashes)
        return self

    def check_lockfile_matches(
        self,
        bucket: RenderBucket,
        node: Node,
        drift_root: Path,
    ) -> Optional[NodeHashes]:
        """Checks if a node matches the recorded lockfile Merkle hash for the bucket.

        Returns NodeHashes(own_hash, merkle_hash) on a cache hit.
        Returns None if missing on disk, unhashable, or not in the lockfile bucket.
        """
        from .render_cache import NodeHashes
        from .render_hasher import hash_file_disk, hash_directory_disk, hash_text
        from .render_dag import FileNode, DirectoryNode

        if isinstance(node, FileNode):
            disk_path = drift_root / node.file_path
            if not disk_path.is_file():
                return None
            own_h = hash_file_disk(node.file_path, drift_root)
            if own_h is None:
                return None
            if any(d.merkle_hash is None for d in node.depends_on):
                return None
            dep_hashes = [d.merkle_hash for d in node.depends_on if d.merkle_hash is not None]
            dep_str = ":".join(dep_hashes)
            candidate_m = (
                hash_text(f"{node.__class__.__name__}:{own_h}:{dep_str}")
                if dep_str
                else hash_text(f"{node.__class__.__name__}:{own_h}")
            )
            if candidate_m in self.get_bucket_hashes(bucket):
                return NodeHashes(own_hash=own_h, merkle_hash=candidate_m)
            return None

        elif isinstance(node, DirectoryNode):
            disk_path = drift_root / node.dir_path
            if not disk_path.is_dir():
                return None
            own_h = hash_directory_disk(node.dir_path, drift_root)
            if own_h is None:
                return None
            candidate_m = hash_text(f"DirectoryNode:{own_h}")
            if candidate_m in self.get_bucket_hashes(bucket):
                return NodeHashes(own_hash=own_h, merkle_hash=candidate_m)
            return None

        return None

    def contains_hash(self, hash_str: str) -> bool:
        """Returns True if the given hash exists in any of the 3 buckets."""
        return (
            hash_str in self.config_hashes
            or hash_str in self.hook_hashes
            or hash_str in self.payload_hashes
        )

    def get_all_hashes(self) -> Set[str]:
        """Returns the combined set of all hashes across all 3 buckets."""
        return self.config_hashes | self.hook_hashes | self.payload_hashes
