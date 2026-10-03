"""Deterministic cryptographic hashing and Merkle tree hash calculations.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Node Hashing & Merkle Invariants
    - compute_node_own_hash(node, base_dir) -> Optional[str]
    - compute_merkle_node_hash(node, base_dir) -> Optional[str]

Layer 1: Low-Level Disk & Byte Hashes
    - hash_bytes(data) -> str
    - hash_text(text) -> str
    - hash_file_disk(file_path, base_dir) -> Optional[str]
    - hash_directory_disk(dir_path, base_dir) -> Optional[str]
===============================================================================
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from .render_dag import (
    Node,
    FileNode,
    IndependentFileNode,
    DirectoryNode,
    PackagePayloadNode,
    PackageHooksNode,
)


# =====================================================================
# Layer 1: Low-Level Disk & Byte Hashes
# =====================================================================

def hash_bytes(data: bytes) -> str:
    """Computes a deterministic SHA-256 hex digest over raw bytes."""
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Computes a deterministic SHA-256 hex digest over UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file_disk(file_path: Path) -> Optional[str]:
    """Computes SHA-256 hash of a file on disk combining relative path, mode, and content.

    Returns None if the file does not exist or is not a regular file.
    """
    if not file_path.is_file():
        return None
    mode_oct = oct(file_path.stat().st_mode & 0o777)
    content = file_path.read_bytes()
    hasher = hashlib.sha256()
    hasher.update(file_path.as_posix().encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(mode_oct.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(content)
    return hasher.hexdigest()


def hash_directory_disk(dir_path: Path) -> Optional[str]:
    """Computes SHA-256 hash of a directory on disk combining relative path and mode.

    Returns None if the path does not exist or is not a directory.
    """
    if not dir_path.is_dir():
        return None
    mode_oct = oct(dir_path.stat().st_mode & 0o777)
    hasher = hashlib.sha256()
    hasher.update(b"DIR\0")
    hasher.update(dir_path.as_posix().encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(mode_oct.encode("utf-8"))
    return hasher.hexdigest()


# =====================================================================
# Layer 2: Node Hashing & Merkle Invariants
# =====================================================================

def compute_node_own_hash(node: Node) -> Optional[str]:
    """Computes the own_hash for a given Node from disk or raw content.

    - TextNode / JsonNode / CachedNode: returns existing own_hash.
    - IndependentFileNode: hashes source asset from disk.
    - FileNode (StaticFileNode, EngineOutputFileNode, PackageConfigNode):
      hashes rendered target artifact from disk (returns None if not yet rendered).
    - DirectoryNode: hashes directory presence and permissions on disk.
    - PackageHooksNode / PackagePayloadNode: hashes type name and package name.
    """
    if node.own_hash is not None:
        return node.own_hash

    if isinstance(node, DirectoryNode):
        return hash_directory_disk(node.dst_path)
    elif isinstance(node, FileNode):
        return hash_file_disk(node.dst_path)
    elif isinstance(node, (PackageHooksNode, PackagePayloadNode)):
        return hash_text(f"{node.__class__.__name__}:{node.value}")
    else:
        return hash_text(node.value)


def compute_merkle_node_hash(node: Node) -> Optional[str]:
    """Computes and populates NodeHashes for a given Node using its dependencies.

    Returns None if the node's own_hash cannot be resolved (e.g. unrendered on disk)
    or if any prerequisite in depends_on has an unresolved merkle_hash.
    """
    if node.merkle_hash is not None:
        return node.merkle_hash

    own_h = node.own_hash if node.own_hash is not None else compute_node_own_hash(node)
    if own_h is None:
        return None

    if any(dep.merkle_hash is None for dep in node.depends_on):
        return None

    dep_hashes = [dep.merkle_hash for dep in node.depends_on if dep.merkle_hash is not None]
    if not dep_hashes:
        m_hash = hash_text(f"{node.__class__.__name__}:{own_h}")
    else:
        deps_str = ":".join(dep_hashes)
        m_hash = hash_text(f"{node.__class__.__name__}:{own_h}:{deps_str}")

    from .render_cache import NodeHashes
    node.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_hash)
    return m_hash
