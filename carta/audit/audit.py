"""Audit pipeline: detect inconsistencies across files, sidecars, and Qdrant.

This module provides structured detection of six issue categories:
- orphaned_chunks: chunks in Qdrant with no matching sidecar
- missing_sidecars: files without .embed-meta.yaml but have chunks
- stale_sidecars: sidecars with mtime older than actual file
- hash_mismatches: file hash differs from sidecar record
- disconnected_files: files with no sidecar and no chunks
- qdrant_sidecar_mismatches: chunks don't align with sidecar metadata
"""

import fnmatch
from pathlib import Path
from datetime import datetime
from qdrant_client import QdrantClient

import yaml


def _build_sidecar_registry(repo_root: Path, cfg: dict) -> dict:
    """Build registry of all sidecars on disk.

    Scans docs_root for .embed-meta.yaml files, respecting excluded_paths.

    Args:
        repo_root: Repository root path
        cfg: Carta config dict with docs_root and excluded_paths

    Returns:
        Dict mapping sidecar_id -> {
            "path": Path to sidecar file,
            "data": Parsed YAML content,
            "file_path": Path to corresponding source file (if exists)
        }
    """
    registry = {}
    docs_root = repo_root / cfg.get("docs_root", "docs")
    excluded = cfg.get("excluded_paths", [])

    if not docs_root.exists():
        return registry

    # Scan for all .embed-meta.yaml files
    for sidecar_path in docs_root.rglob("*.embed-meta.yaml"):
        # Check if excluded
        rel_path = sidecar_path.relative_to(repo_root)
        # Normalize path for pattern matching (use forward slashes)
        rel_path_str = str(rel_path).replace("\\", "/")
        # Try both exact match and wildcard match for excluded patterns
        is_excluded = False
        for pattern in excluded:
            # Match if pattern is in path (with wildcards support)
            if fnmatch.fnmatch(rel_path_str, pattern) or fnmatch.fnmatch(rel_path_str, f"*/{pattern}*"):
                is_excluded = True
                break
        if is_excluded:
            continue

        # Load sidecar
        try:
            sidecar_data = yaml.safe_load(sidecar_path.read_text())
            if not sidecar_data or "sidecar_id" not in sidecar_data:
                continue

            sidecar_id = sidecar_data["sidecar_id"]
            source_file = sidecar_path.with_suffix("") if sidecar_path.suffix == ".yaml" else None

            # Adjust: .embed-meta.yaml means source is without .embed-meta.yaml
            # e.g., test.md.embed-meta.yaml -> test.md
            source_file = Path(str(sidecar_path).replace(".embed-meta.yaml", ""))

            registry[sidecar_id] = {
                "path": sidecar_path,
                "data": sidecar_data,
                "file_path": source_file if source_file.exists() else None
            }
        except Exception:
            # Skip malformed sidecars
            continue

    return registry


def _build_qdrant_chunk_index(client: QdrantClient, collection_name: str) -> dict:
    """Index all chunks in Qdrant by sidecar_id.

    Args:
        client: Connected Qdrant client
        collection_name: Collection to scan

    Returns:
        Dict mapping sidecar_id -> [chunk_records]
    """
    pass


def detect_orphaned_chunks(client: QdrantClient, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect chunks in Qdrant with no matching sidecar on disk.

    Args:
        client: Connected Qdrant client
        cfg: Carta config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry
        qdrant_index: Chunk index from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="orphaned_chunks"
    """
    pass


def detect_missing_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect files with chunks in Qdrant but no sidecar.

    Args:
        repo_root: Repository root path
        cfg: Carta config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry
        qdrant_index: Chunk index from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="missing_sidecars"
    """
    pass


def detect_stale_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect sidecars where file mtime is newer than last embed.

    Args:
        repo_root: Repository root path
        cfg: Carta config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry

    Returns:
        List of issue dicts with category="stale_sidecars"
    """
    pass


def detect_hash_mismatches(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect files where computed hash differs from sidecar record.

    Args:
        repo_root: Repository root path
        cfg: Carta config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry

    Returns:
        List of issue dicts with category="hash_mismatches"
    """
    pass


def detect_disconnected_files(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect discoverable files with no sidecar and no chunks.

    Args:
        repo_root: Repository root path
        cfg: Carta config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry
        qdrant_index: Chunk index from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="disconnected_files"
    """
    pass


def detect_qdrant_sidecar_mismatches(client: QdrantClient, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect chunks in Qdrant that don't match sidecar metadata.

    Args:
        client: Connected Qdrant client
        cfg: Carta config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry
        qdrant_index: Chunk index from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="qdrant_sidecar_mismatches"
    """
    pass


def run_audit(cfg: dict, repo_root: Path, verbose: bool = False) -> dict:
    """Run full audit and return results dict matching JSON schema.

    Args:
        cfg: Carta config dict
        repo_root: Repository root path
        verbose: If True, print detailed progress messages

    Returns:
        Audit results dict with keys: issues, summary, timestamp
    """
    pass
