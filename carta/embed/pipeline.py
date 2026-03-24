"""Top-level pipeline orchestration for carta embed."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from qdrant_client import QdrantClient
from qdrant_client.models import Filter

from carta.config import collection_name
from carta.embed.parse import extract_pdf_text, chunk_text
from carta.embed.embed import ensure_collection, upsert_chunks, get_embedding
from carta.embed.induct import read_sidecar


_SUPPORTED_EXTENSIONS = [".pdf"]


def is_lfs_pointer(file_path: Path) -> bool:
    """Check if a file is a Git LFS pointer (not actual content)."""
    try:
        head = file_path.read_bytes()[:128]
        return head.startswith(b"version https://git-lfs.github.com/spec/v1")
    except (OSError, IOError):
        return False


def _update_sidecar(sidecar_path: Path, updates: dict) -> None:
    """Merge updates into an existing sidecar file."""
    data = read_sidecar(sidecar_path) or {}
    data.update(updates)
    with open(sidecar_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def discover_pending_files(repo_root: Path, cfg: dict) -> list[dict]:
    """Find all .embed-meta.yaml sidecars with status: pending under repo_root.

    Returns list of dicts: sidecar data + 'sidecar_path' + 'file_path'.
    """
    results = []
    for sidecar_path in repo_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sidecar_path)
        if data is None:
            continue
        if data.get("status") != "pending":
            continue

        stem = sidecar_path.name.replace(".embed-meta.yaml", "")
        parent = sidecar_path.parent
        source_file = None
        for ext in _SUPPORTED_EXTENSIONS:
            candidate = parent / f"{stem}{ext}"
            if candidate.exists():
                source_file = candidate
                break

        if source_file:
            results.append({
                **data,
                "sidecar_path": sidecar_path,
                "file_path": source_file,
            })

    return results


def run_embed(repo_root: Path, cfg: dict) -> dict:
    """Run the embed pipeline on all pending files under repo_root.

    Args:
        repo_root: root directory to scan for .embed-meta.yaml sidecars.
        cfg: carta config dict.

    Returns:
        {"embedded": int, "skipped": int, "errors": list[str]}
    """
    summary: dict = {"embedded": 0, "skipped": 0, "errors": []}

    # Check Qdrant reachability
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=5)
        client.get_collections()
    except Exception as e:
        summary["errors"].append(f"[Qdrant: skipped — unreachable] {e}")
        return summary

    coll_name = collection_name(cfg, "doc")
    ensure_collection(client, coll_name)

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 800)
    overlap_fraction = chunking.get("overlap_fraction", 0.15)
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["embed"]["ollama_model"]

    pending = discover_pending_files(repo_root, cfg)

    for file_info in pending:
        file_path: Path = file_info["file_path"]
        sidecar_path: Path = file_info["sidecar_path"]

        # LFS guard
        if is_lfs_pointer(file_path):
            summary["skipped"] += 1
            continue

        try:
            pages = extract_pdf_text(file_path)
            raw_chunks = chunk_text(pages, max_tokens=max_tokens, overlap_fraction=overlap_fraction)

            slug = file_info.get("slug", file_path.stem)
            metadata = {
                "slug": slug,
                "file_path": str(file_path.relative_to(repo_root)),
                "doc_type": file_info.get("doc_type", "unknown"),
            }

            # Build enriched chunks for upsert_chunks (needs slug + chunk_index)
            enriched = [
                {**metadata, **chunk}
                for chunk in raw_chunks
            ]

            count = upsert_chunks(enriched, cfg, client=client)

            _update_sidecar(sidecar_path, {
                "status": "embedded",
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "chunk_count": count,
            })
            summary["embedded"] += 1

        except Exception as e:
            summary["errors"].append(f"Error processing {file_path.name}: {e}")

    return summary


def run_search(query: str, cfg: dict) -> list[dict]:
    """Search the doc collection for chunks semantically similar to query.

    Args:
        query: natural-language search query.
        cfg: carta config dict.

    Returns:
        List of dicts: {"score": float, "source": str, "excerpt": str}
        Ordered by descending similarity score.
    """
    top_n = cfg.get("search", {}).get("top_n", 5)
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["embed"]["ollama_model"]

    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e

    query_vec = get_embedding(query, ollama_url=ollama_url, model=model, prefix="search_query: ")

    coll_name = collection_name(cfg, "doc")

    try:
        response = client.query_points(
            collection_name=coll_name,
            query=query_vec,
            limit=top_n,
            with_payload=True,
        )
    except Exception as e:
        print(f"Warning: search failed: {e}", file=sys.stderr)
        return []

    hits = []
    for r in response.points:
        payload = r.payload or {}
        hits.append({
            "score": r.score,
            "source": payload.get("file_path", payload.get("slug", "")),
            "excerpt": payload.get("text", ""),
        })
    return hits
