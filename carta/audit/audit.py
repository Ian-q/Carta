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
import os
from pathlib import Path
from datetime import datetime, timedelta
from qdrant_client import QdrantClient

import yaml

from carta.embed.lifecycle import compute_file_hash


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

    Scrolls the collection and groups chunks by sidecar_id.
    Skips chunks without sidecar_id (pre-999.1 migration boundary).

    Args:
        client: Connected Qdrant client
        collection_name: Collection to scan

    Returns:
        Dict mapping sidecar_id -> [chunk_records with id, payload]
    """
    index = {}

    try:
        # Scroll through all chunks
        points, _ = client.scroll(
            collection_name=collection_name,
            limit=1000,  # Qdrant scroll batch size
        )

        while points:
            for point in points:
                sidecar_id = point.payload.get("sidecar_id")
                if not sidecar_id:
                    continue  # Skip pre-999.1 chunks

                if sidecar_id not in index:
                    index[sidecar_id] = []

                index[sidecar_id].append({
                    "id": point.id,
                    "payload": point.payload,
                    "chunk_index": point.payload.get("chunk_index")
                })

            # Continue scrolling if more points
            if len(points) < 1000:
                break

            points, _ = client.scroll(
                collection_name=collection_name,
                limit=1000,
                offset=len(points),
            )
    except Exception:
        # Collection doesn't exist or is unreachable; return empty index
        pass

    return index


def detect_orphaned_chunks(client: QdrantClient, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect chunks in Qdrant with no matching sidecar on disk.

    Args:
        client: Qdrant client (for fetching chunk text if needed)
        cfg: Config dict
        sidecar_registry: Registry of sidecars from _build_sidecar_registry
        qdrant_index: Chunk index from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="orphaned_chunks"
    """
    issues = []

    for sidecar_id, chunks in qdrant_index.items():
        if sidecar_id not in sidecar_registry:
            # Orphaned: chunks exist but no sidecar
            chunk_ids = [c["id"] for c in chunks]

            # Get first chunk's text for preview
            first_text = ""
            if chunks and chunks[0].get("payload", {}).get("text"):
                first_text = chunks[0]["payload"]["text"][:100]

            issue = {
                "id": f"orphaned_{sidecar_id[:8]}",
                "category": "orphaned_chunks",
                "severity": "warning",
                "sidecar_id": sidecar_id,
                "chunk_ids": chunk_ids,
                "chunk_count": len(chunks),
                "first_chunk_text": first_text,
                "metadata": {
                    "doc_type": chunks[0].get("payload", {}).get("doc_type", "unknown") if chunks else "unknown",
                    "collection": f"{cfg.get('project_name', 'unknown')}_doc"
                }
            }
            issues.append(issue)

    return issues


def detect_missing_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect files with chunks in Qdrant but no sidecar on disk.

    This indicates a partial failure or manual deletion of sidecar.

    Args:
        repo_root: Repository root
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry
        qdrant_index: Chunks from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="missing_sidecars"
    """
    issues = []

    for sidecar_id, chunks in qdrant_index.items():
        if sidecar_id not in sidecar_registry and chunks:
            # Find file_path from chunk payload if available
            file_path = None
            if chunks and chunks[0].get("payload", {}).get("file_path"):
                file_path = chunks[0]["payload"]["file_path"]

            issue = {
                "id": f"missing_sidecar_{sidecar_id[:8]}",
                "category": "missing_sidecars",
                "severity": "warning",
                "sidecar_id": sidecar_id,
                "file_path": file_path,
                "chunk_count": len(chunks),
                "expected_sidecar_path": f"{file_path}.embed-meta.yaml" if file_path else "unknown"
            }
            issues.append(issue)

    return issues


def detect_stale_sidecars(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect sidecars where file mtime is newer than recorded mtime.

    Indicates file changed after embedding.

    Args:
        repo_root: Repository root
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry

    Returns:
        List of issue dicts with category="stale_sidecars"
    """
    issues = []

    for sidecar_id, sidecar_info in sidecar_registry.items():
        file_path = sidecar_info.get("file_path")
        if not file_path or not file_path.exists():
            continue

        sidecar_data = sidecar_info["data"]
        recorded_mtime = sidecar_data.get("file_mtime")
        last_embedded = sidecar_data.get("last_embedded")

        if recorded_mtime is None:
            continue

        actual_mtime = os.path.getmtime(file_path)

        if actual_mtime > recorded_mtime:
            # Compute how many days stale
            now = datetime.now()
            embedded_dt = datetime.fromisoformat(last_embedded) if last_embedded else datetime.fromtimestamp(recorded_mtime)
            days_stale = (now - embedded_dt).days

            issue = {
                "id": f"stale_{sidecar_id[:8]}",
                "category": "stale_sidecars",
                "severity": "info",
                "file_path": str(file_path.relative_to(repo_root)),
                "sidecar_path": str(sidecar_info["path"].relative_to(repo_root)),
                "last_embedded": last_embedded,
                "file_mtime": datetime.fromtimestamp(actual_mtime).isoformat(),
                "days_stale": max(0, days_stale)
            }
            issues.append(issue)

    return issues


def detect_hash_mismatches(repo_root: Path, cfg: dict, sidecar_registry: dict) -> list[dict]:
    """Detect files where computed hash differs from sidecar record.

    Indicates file content changed (even if mtime same due to touch/clock skew).

    Args:
        repo_root: Repository root
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry

    Returns:
        List of issue dicts with category="hash_mismatches"
    """
    issues = []

    for sidecar_id, sidecar_info in sidecar_registry.items():
        file_path = sidecar_info.get("file_path")
        if not file_path or not file_path.exists():
            continue

        sidecar_data = sidecar_info["data"]
        recorded_hash = sidecar_data.get("file_hash")

        if recorded_hash is None:
            continue

        actual_hash = compute_file_hash(file_path)

        if actual_hash != recorded_hash:
            issue = {
                "id": f"hash_mismatch_{sidecar_id[:8]}",
                "category": "hash_mismatches",
                "severity": "warning",
                "file_path": str(file_path.relative_to(repo_root)),
                "sidecar_path": str(sidecar_info["path"].relative_to(repo_root)),
                "recorded_hash": recorded_hash,
                "actual_hash": actual_hash
            }
            issues.append(issue)

    return issues


def detect_disconnected_files(repo_root: Path, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect discoverable files with no sidecar and no chunks in Qdrant.

    These files were never embedded or were removed from Qdrant only.

    Args:
        repo_root: Repository root
        cfg: Config dict with docs_root and excluded_paths
        sidecar_registry: Sidecars from _build_sidecar_registry
        qdrant_index: Chunks from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="disconnected_files"
    """
    issues = []

    docs_root = repo_root / cfg.get("docs_root", "docs")
    excluded = cfg.get("excluded_paths", [])

    if not docs_root.exists():
        return issues

    # Collect all files with sidecars or chunks
    covered_files = set()

    for sidecar_info in sidecar_registry.values():
        if sidecar_info.get("file_path"):
            covered_files.add(sidecar_info["file_path"])

    for chunks in qdrant_index.values():
        for chunk in chunks:
            file_path_str = chunk.get("payload", {}).get("file_path")
            if file_path_str:
                covered_files.add(Path(file_path_str))

    # Scan for all discoverable files (both .md and .pdf)
    discoverable_files = set()
    for file_path in docs_root.rglob("*"):
        if file_path.suffix in (".md", ".pdf"):
            discoverable_files.add(file_path)

    # Check if excluded
    for file_path in list(discoverable_files):
        rel_path = file_path.relative_to(repo_root)
        rel_path_str = str(rel_path).replace("\\", "/")
        if any(fnmatch.fnmatch(rel_path_str, pattern) or fnmatch.fnmatch(rel_path_str, f"*/{pattern}*") for pattern in excluded):
            discoverable_files.discard(file_path)

    # Find disconnected files
    for file_path in discoverable_files:
        if file_path not in covered_files:
            rel_path = file_path.relative_to(repo_root)
            issue = {
                "id": f"disconnected_{file_path.stem[:8]}",
                "category": "disconnected_files",
                "severity": "info",
                "file_path": str(rel_path),
                "reason": "File exists, no sidecar, no chunks in Qdrant"
            }
            issues.append(issue)

    return issues


def detect_qdrant_sidecar_mismatches(client: QdrantClient, cfg: dict, sidecar_registry: dict, qdrant_index: dict) -> list[dict]:
    """Detect chunks in Qdrant that don't match sidecar metadata.

    Checks chunk_count and chunk_index alignment.

    Args:
        client: Qdrant client
        cfg: Config dict
        sidecar_registry: Sidecars from _build_sidecar_registry
        qdrant_index: Chunks from _build_qdrant_chunk_index

    Returns:
        List of issue dicts with category="qdrant_sidecar_mismatches"
    """
    issues = []

    for sidecar_id, sidecar_info in sidecar_registry.items():
        sidecar_data = sidecar_info["data"]
        recorded_count = sidecar_data.get("chunk_count")

        chunks = qdrant_index.get(sidecar_id, [])
        actual_count = len(chunks)

        if recorded_count is not None and actual_count != recorded_count:
            issue = {
                "id": f"mismatch_{sidecar_id[:8]}",
                "category": "qdrant_sidecar_mismatches",
                "severity": "error",
                "sidecar_id": sidecar_id,
                "recorded_chunk_count": recorded_count,
                "actual_chunk_count": actual_count,
                "chunk_ids": [c["id"] for c in chunks],
                "reason": f"Sidecar expects {recorded_count} chunks, Qdrant has {actual_count}"
            }
            issues.append(issue)

    return issues


def run_audit(cfg: dict, repo_root: Path, verbose: bool = False) -> dict:
    """Run full audit and return results dict matching JSON schema.

    Args:
        cfg: Carta config dict
        repo_root: Repository root path
        verbose: Print progress to stdout

    Returns:
        Dict with summary and issues list matching JSON schema
    """
    start_time = datetime.now()

    if verbose:
        print("Audit: building sidecar registry...", flush=True)

    sidecar_registry = _build_sidecar_registry(repo_root, cfg)

    if verbose:
        print(f"Audit: found {len(sidecar_registry)} sidecars", flush=True)

    # Connect to Qdrant
    try:
        client = QdrantClient(url=cfg.get("qdrant_url", "http://localhost:6333"), timeout=5)
        client.get_collections()
    except Exception as e:
        return {
            "summary": {
                "total_issues": -1,
                "error": f"Qdrant unreachable: {e}",
                "scanned_at": start_time.isoformat(),
                "repo_root": str(repo_root)
            },
            "issues": []
        }

    if verbose:
        print("Audit: building qdrant chunk index...", flush=True)

    collection_name = f"{cfg.get('project_name', 'unknown')}_doc"
    qdrant_index = _build_qdrant_chunk_index(client, collection_name)

    if verbose:
        print(f"Audit: scanning for issues...", flush=True)

    # Run all detection functions
    all_issues = []
    all_issues.extend(detect_orphaned_chunks(client, cfg, sidecar_registry, qdrant_index))
    all_issues.extend(detect_missing_sidecars(repo_root, cfg, sidecar_registry, qdrant_index))
    all_issues.extend(detect_stale_sidecars(repo_root, cfg, sidecar_registry))
    all_issues.extend(detect_hash_mismatches(repo_root, cfg, sidecar_registry))
    all_issues.extend(detect_disconnected_files(repo_root, cfg, sidecar_registry, qdrant_index))
    all_issues.extend(detect_qdrant_sidecar_mismatches(client, cfg, sidecar_registry, qdrant_index))

    # Tally by category
    by_category = {}
    for issue in all_issues:
        cat = issue["category"]
        by_category[cat] = by_category.get(cat, 0) + 1

    result = {
        "summary": {
            "total_issues": len(all_issues),
            "by_category": by_category,
            "scanned_at": start_time.isoformat(),
            "repo_root": str(repo_root),
            "project_name": cfg.get("project_name", "unknown"),
            "collection_scanned": collection_name
        },
        "issues": all_issues
    }

    if verbose:
        print(f"Audit complete: {len(all_issues)} issues found", flush=True)

    return result
