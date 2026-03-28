"""Pure-stdlib hash and mtime primitives for embed lifecycle tracking.

This module provides the decision-making core of the embed pipeline:
- Hash computation with LF-normalization for markdown, raw bytes for PDF
- mtime fast-path to skip rehashing when file unchanged

No Qdrant or external dependencies — fully testable in isolation.
"""

import hashlib
import os
from pathlib import Path


def compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of a file, with LF-normalization for markdown.

    Args:
        path: Path to the file to hash.

    Returns:
        Lowercase hexadecimal SHA256 hash string (64 characters).

    Behavior:
        - Markdown (.md) files: read_bytes(), normalize CRLF → LF, hash
        - PDF (.pdf) files: read_bytes() raw, hash without normalization
        - Other text files: treat as markdown (LF-normalized)
    """
    raw_bytes = path.read_bytes()

    # Determine file type based on extension
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        # PDF: hash raw bytes without normalization
        hash_obj = hashlib.sha256(raw_bytes)
    else:
        # Markdown and other text files: normalize CRLF → LF
        normalized = raw_bytes.replace(b"\r\n", b"\n")
        hash_obj = hashlib.sha256(normalized)

    return hash_obj.hexdigest()


def needs_rehash(file_path: Path, sidecar: dict) -> bool:
    """Check if a file needs rehashing based on mtime fast-path.

    Args:
        file_path: Path to the file to check.
        sidecar: Sidecar metadata dict (from .embed-meta.yaml).

    Returns:
        True if file needs rehashing, False if mtime unchanged.

    Behavior:
        - Returns True if sidecar has no "file_mtime" key (never hashed before)
        - Returns False if actual mtime matches sidecar["file_mtime"] (no change)
        - Returns True if mtime values differ (file has changed)
    """
    if "file_mtime" not in sidecar:
        return True

    actual_mtime = os.path.getmtime(file_path)
    return actual_mtime != sidecar["file_mtime"]
