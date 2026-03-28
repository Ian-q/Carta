"""Pure-stdlib hash and mtime primitives + Qdrant lifecycle functions for embed tracking.

This module provides the decision-making core of the embed pipeline:
- Hash computation with LF-normalization for markdown, raw bytes for PDF
- mtime fast-path to skip rehashing when file unchanged
- Qdrant lifecycle ops: mark stale, cleanup orphans, protect doc types, alert on staleness

Leaf module: lifecycle.py imports from qdrant_client but is NOT imported by other
carta.embed modules (no circular dependencies).
"""

import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

from qdrant_client.models import FieldCondition, Filter, MatchValue, DatetimeRange


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


def mark_sidecar_stale(
    client, collection_name: str, sidecar_id: str | None, now: datetime
) -> None:
    """Mark all chunks with a given sidecar_id as stale in Qdrant.

    Args:
        client: Qdrant client instance.
        collection_name: Qdrant collection name.
        sidecar_id: Sidecar ID to match. If empty or None, this is a no-op (pre-999.1 guard).
        now: Current datetime to record as stale_as_of.

    Behavior:
        - If sidecar_id is empty string or None, returns without calling client (pre-999.1 migration boundary)
        - Otherwise, calls client.set_payload with Filter on sidecar_id
        - Sets payload={stale_as_of: now.isoformat(), status: "stale"}
    """
    if not sidecar_id:  # Empty string or None
        return

    filter_condition = Filter(
        must=[FieldCondition(key="sidecar_id", match=MatchValue(value=sidecar_id))]
    )

    client.set_payload(
        collection_name=collection_name,
        payload={"stale_as_of": now.isoformat(), "status": "stale"},
        points_selector=filter_condition,
    )


def cleanup_expired_orphans(
    client, collection_name: str, ttl_days: int, now: datetime
) -> int:
    """Delete orphaned chunks older than TTL from Qdrant.

    Args:
        client: Qdrant client instance.
        collection_name: Qdrant collection name.
        ttl_days: Time-to-live in days. Chunks with orphaned_at < (now - ttl_days) are deleted.
        now: Current datetime for cutoff calculation.

    Returns:
        Count of deleted points.

    Behavior:
        - Calculates cutoff = now - timedelta(days=ttl_days)
        - Scrolls Qdrant with DatetimeRange filter on orphaned_at field
        - GUARD: Filters out any points where sidecar_id is absent or empty (pre-999.1 chunks)
        - Deletes only points that pass the sidecar_id guard
        - Returns count of deleted points; returns 0 if scroll is empty
    """
    cutoff = now - timedelta(days=ttl_days)

    filter_condition = Filter(
        must=[FieldCondition(key="orphaned_at", range=DatetimeRange(lt=cutoff))]
    )

    points, _ = client.scroll(
        collection_name=collection_name,
        limit=100,
        scroll_filter=filter_condition,
    )

    if not points:
        return 0

    # Filter out pre-999.1 chunks (missing sidecar_id)
    eligible_point_ids = [
        p.id for p in points if p.payload.get("sidecar_id")
    ]

    if not eligible_point_ids:
        return 0

    client.delete(collection_name=collection_name, points_selector=eligible_point_ids)
    return len(eligible_point_ids)


# Protected doc types that should never be deleted by orphan cleanup
PROTECTED_DOC_TYPES = frozenset({"quirk", "bug-note", "helpful-note"})


def is_protected_doc_type(doc_type: str) -> bool:
    """Check if a doc_type is protected from orphan cleanup.

    Args:
        doc_type: The document type string.

    Returns:
        True if doc_type is in the protected set, False otherwise.

    Behavior:
        - Protected types: "quirk", "bug-note", "helpful-note"
        - All other types return False
    """
    return doc_type in PROTECTED_DOC_TYPES


def check_stale_alert(stale_count: int, total_count: int, threshold: float) -> str | None:
    """Generate a warning if stale chunk percentage exceeds threshold.

    Args:
        stale_count: Number of stale chunks.
        total_count: Total number of chunks.
        threshold: Fraction (0.30 = 30%) to trigger alert.

    Returns:
        Warning string if stale_count / total_count >= threshold, None otherwise.

    Behavior:
        - Returns None if total_count == 0 (division by zero guard)
        - Returns None if stale_count / total_count < threshold
        - Returns warning message with percentage and "carta embed --force-stale" hint otherwise
    """
    if total_count == 0:
        return None

    percentage = (stale_count / total_count) * 100
    if percentage < threshold * 100:
        return None

    percent_int = int(percentage)
    return (
        f"Warning: {percent_int}% of embedded docs are stale. "
        f"Run 'carta embed --force-stale' to refresh them."
    )
