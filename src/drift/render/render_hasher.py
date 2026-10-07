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
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from ..utils.path_utils import to_relative_posix
from .render_dag import (
    Node,
    FileNode,
    IndependentFileNode,
    DirectoryNode,
    PackagePayloadNode,
    PackageHooksNode,
)


def format_hash_log(hash_val: Any, length: int = 10) -> str:
    """Formats a hash or hash collection into a shortened string (first N chars, default 10) for clean log presentation."""
    if hash_val is None:
        return "none"
    if isinstance(hash_val, str):
        if not hash_val:
            return "none"
        if ":" in hash_val:
            return ":".join(format_hash_log(part, length) for part in hash_val.split(":"))
        return hash_val[:length]
    if isinstance(hash_val, (list, tuple, set)):
        return "[" + ", ".join(format_hash_log(item, length) for item in hash_val) + "]"
    return str(hash_val)[:length]


# =====================================================================
# Layer 1: Low-Level Disk & Byte Hashes
# =====================================================================

def resolve_hash_path_key(path: Path, package_render_dir: Optional[Path] = None) -> str:
    """Returns package-relative POSIX path if within package_render_dir, or absolute POSIX path otherwise."""
    if package_render_dir is not None:
        return to_relative_posix(path.resolve(), package_render_dir.resolve())
    return path.resolve().as_posix()


def hash_bytes(data: bytes) -> str:
    """Computes a deterministic SHA-256 hex digest over raw bytes."""
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    """Computes a deterministic SHA-256 hex digest over UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file_disk(file_path: Path, package_render_dir: Optional[Path] = None) -> Optional[str]:
    """Computes SHA-256 hash of a file on disk combining path, mode, and content.

    If package_render_dir is provided and file_path is within it, uses the package-relative
    POSIX path for the path segment of the hash; otherwise uses the absolute POSIX path.
    Returns None if the file does not exist or is not a regular file.
    """
    if not file_path.is_file():
        return None
    mode_oct = oct(file_path.stat().st_mode & 0o777)
    content = file_path.read_bytes()
    path_key = resolve_hash_path_key(file_path, package_render_dir)
    hasher = hashlib.sha256()
    hasher.update(path_key.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(mode_oct.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(content)
    return hasher.hexdigest()


def hash_directory_disk(dir_path: Path, package_render_dir: Optional[Path] = None) -> Optional[str]:
    """Computes SHA-256 hash of a directory on disk combining path and mode.

    If package_render_dir is provided and dir_path is within it, uses the package-relative
    POSIX path for the path segment of the hash; otherwise uses the absolute POSIX path.
    Returns None if the path does not exist or is not a directory.
    """
    if not dir_path.is_dir():
        return None
    mode_oct = oct(dir_path.stat().st_mode & 0o777)
    path_key = resolve_hash_path_key(dir_path, package_render_dir)
    hasher = hashlib.sha256()
    hasher.update(b"DIR\0")
    hasher.update(path_key.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(mode_oct.encode("utf-8"))
    return hasher.hexdigest()


# =====================================================================
# Layer 2: Node Hashing & Merkle Invariants
# =====================================================================

def compute_node_own_hash(node: Node, package_render_dir: Optional[Path] = None) -> Optional[str]:
    """Computes the own_hash for a given Node from disk or raw content.

    - TextNode / JsonNode / CachedNode: returns existing own_hash.
    - IndependentFileNode: hashes source asset from disk (absolute path).
    - FileNode (StaticFileNode, EngineOutputFileNode, PackageConfigNode):
      hashes rendered target artifact from disk (package-relative if inside package_render_dir).
    - DirectoryNode: hashes directory presence and permissions on disk.
    - PackageHooksNode / PackagePayloadNode: hashes type name and package name.
    """
    if node.own_hash is not None:
        return node.own_hash

    if isinstance(node, DirectoryNode):
        return hash_directory_disk(node.dst_path, package_render_dir=package_render_dir)
    elif isinstance(node, FileNode):
        return hash_file_disk(node.dst_path, package_render_dir=package_render_dir)
    elif isinstance(node, (PackageHooksNode, PackagePayloadNode)):
        return hash_text(f"{node.__class__.__name__}:{node.value}")
    else:
        return hash_text(node.value)


def compute_merkle_node_hash(node: Node, package_render_dir: Optional[Path] = None) -> Optional[str]:
    """Computes and populates NodeHashes for a given Node using its dependencies.

    Returns None if the node's own_hash cannot be resolved (e.g. unrendered on disk)
    or if any prerequisite in depends_on has an unresolved merkle_hash.
    """
    if node.merkle_hash is not None:
        return node.merkle_hash

    own_h = node.own_hash if node.own_hash is not None else compute_node_own_hash(node, package_render_dir=package_render_dir)
    if own_h is None:
        logger.debug(
            f"[Merkle Hash] Failed to resolve own_hash for {node.__class__.__name__} "
            f"(value='{getattr(node, 'dst_path', getattr(node, 'src_path', node.value))}')."
        )
        return None

    unresolved = [
        getattr(d, "dst_path", getattr(d, "src_path", getattr(d, "value", str(d))))
        for d in node.depends_on
        if d.merkle_hash is None
    ]
    if unresolved:
        logger.debug(
            f"[Merkle Hash] Failed for {node.__class__.__name__} "
            f"('{getattr(node, 'dst_path', node.value)}'): unresolved deps {unresolved}."
        )
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
