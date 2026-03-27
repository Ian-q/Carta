"""Top-level pipeline orchestration for carta embed."""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from qdrant_client import QdrantClient
from qdrant_client.models import Filter

from carta.config import collection_name, find_config
from carta.embed.parse import extract_pdf_text, chunk_text
from carta.embed.embed import ensure_collection, upsert_chunks, get_embedding
from carta.embed.induct import generate_sidecar_stub, read_sidecar, write_sidecar


_SUPPORTED_EXTENSIONS = [".pdf"]

FILE_TIMEOUT_S = 300


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


def discover_pending_files(repo_root: Path) -> list[dict]:
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


def _embed_one_file(
    file_path: Path,
    file_info: dict,
    cfg: dict,
    client,
    repo_root: Path,
    max_tokens: int,
    overlap_fraction: float,
    verbose: bool = False,
) -> tuple:
    """Extract, chunk, embed, and upsert a single file.

    Args:
        file_path: absolute path to the source file.
        file_info: sidecar data dict (slug, doc_type, etc.).
        cfg: carta config dict.
        client: connected QdrantClient.
        repo_root: repo root for relative path computation.
        max_tokens: chunking parameter.
        overlap_fraction: chunking parameter.
        verbose: if True, print progress to stdout.

    Returns:
        Tuple of (chunk_count: int, sidecar_updates: dict).
    """
    pages = extract_pdf_text(file_path)
    raw_chunks = chunk_text(pages, max_tokens=max_tokens, overlap_fraction=overlap_fraction)

    slug = file_info.get("slug", file_path.stem)
    metadata = {
        "slug": slug,
        "file_path": str(file_path.relative_to(repo_root)),
        "doc_type": file_info.get("doc_type", "unknown"),
    }

    enriched = [{**metadata, **chunk} for chunk in raw_chunks]
    count = upsert_chunks(enriched, cfg, client=client)

    sidecar_updates = {
        "status": "embedded",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "chunk_count": count,
        "file_mtime": os.path.getmtime(str(file_path)),
    }
    return count, sidecar_updates


def run_embed_file(path: Path, cfg: dict, force: bool = False, verbose: bool = False) -> dict:
    """Embed a single specified file. Returns status dict.

    Args:
        path: absolute or repo-relative path to the file.
        cfg: carta config dict.
        force: if True, re-embed even if file mtime is unchanged.
        verbose: if True, print progress to stdout.

    Returns:
        {"status": "ok", "chunks": int} on success.
        {"status": "skipped", "reason": str} when file is already current.

    Raises:
        FileNotFoundError: if path does not exist.
        RuntimeError: if Qdrant is unreachable.
    """
    # Resolve repo root from find_config
    cfg_path = find_config()
    repo_root = cfg_path.parent.parent

    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = (repo_root / file_path).resolve()

    if not file_path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    sidecar_path = file_path.parent / (file_path.stem + ".embed-meta.yaml")

    # Check mtime skip (only when sidecar exists and force=False)
    if sidecar_path.exists() and not force:
        sidecar = read_sidecar(sidecar_path)
        if sidecar is not None:
            stored_mtime = sidecar.get("file_mtime")
            if stored_mtime is not None:
                current_mtime = os.path.getmtime(str(file_path))
                if current_mtime == stored_mtime:
                    return {"status": "skipped", "reason": "already embedded, file unchanged"}

    # Generate sidecar if it doesn't exist
    if not sidecar_path.exists():
        stub = generate_sidecar_stub(file_path, repo_root, cfg)
        write_sidecar(file_path, stub)

    # Read sidecar for file_info
    sidecar_data = read_sidecar(sidecar_path) or {}
    file_info = {
        "slug": sidecar_data.get("slug", file_path.stem),
        "doc_type": sidecar_data.get("doc_type", "unknown"),
        "sidecar_path": sidecar_path,
        "file_path": file_path,
    }

    # Connect to Qdrant
    client = QdrantClient(url=cfg["qdrant_url"], timeout=5)
    ensure_collection(client, collection_name(cfg, "doc"))

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 800)
    overlap_fraction = chunking.get("overlap_fraction", 0.15)

    count, sidecar_updates = _embed_one_file(
        file_path, file_info, cfg, client, repo_root, max_tokens, overlap_fraction, verbose
    )
    _update_sidecar(sidecar_path, sidecar_updates)
    return {"status": "ok", "chunks": count}


def run_embed(repo_root: Path, cfg: dict) -> dict:
    """Run the embed pipeline on all pending files under repo_root.

    Args:
        repo_root: root directory to scan for .embed-meta.yaml sidecars.
        cfg: carta config dict.

    Returns:
        {"embedded": int, "skipped": int, "errors": list[str]}
    """
    summary: dict = {"embedded": 0, "skipped": 0, "errors": []}

    # Pre-flight: check Qdrant reachability with a short timeout
    print("carta embed: checking Qdrant connectivity...", flush=True)
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=5)
        client.get_collections()
    except Exception as e:
        err = (
            f"carta embed: ERROR — Qdrant is not reachable at {cfg['qdrant_url']}.\n"
            f"  Is Docker running? Start it and try again.\n"
            f"  Detail: {e}"
        )
        print(err, file=sys.stderr, flush=True)
        summary["errors"].append(err)
        return summary

    coll_name = collection_name(cfg, "doc")
    ensure_collection(client, coll_name)

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 800)
    overlap_fraction = chunking.get("overlap_fraction", 0.15)

    pending = discover_pending_files(repo_root)
    total = len(pending)
    print(f"carta embed: {total} file(s) pending.", flush=True)

    for idx, file_info in enumerate(pending, start=1):
        file_path: Path = file_info["file_path"]
        sidecar_path: Path = file_info["sidecar_path"]

        # LFS guard
        if is_lfs_pointer(file_path):
            print(f"  [{idx}/{total}] SKIP (LFS pointer): {file_path.name}", flush=True)
            summary["skipped"] += 1
            continue

        print(f"  [{idx}/{total}] Embedding: {file_path.name} ...", flush=True)
        t0 = time.monotonic()

        try:
            print(f"  [{idx}/{total}]   extracting and embedding: {file_path.name}...", flush=True)
            count, sidecar_updates = _embed_one_file(
                file_path, file_info, cfg, client, repo_root, max_tokens, overlap_fraction, verbose=False
            )
            _update_sidecar(sidecar_path, sidecar_updates)
            elapsed = time.monotonic() - t0
            print(f"  [{idx}/{total}] OK: {file_path.name} — {count} chunk(s) in {elapsed:.1f}s", flush=True)
            summary["embedded"] += 1

        except Exception as e:
            elapsed = time.monotonic() - t0
            print(f"  [{idx}/{total}] ERROR: {file_path.name} ({elapsed:.1f}s): {e}", file=sys.stderr, flush=True)
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
        raise RuntimeError(f"Qdrant search failed: {e}") from e

    hits = []
    for r in response.points:
        payload = r.payload or {}
        hits.append({
            "score": r.score,
            "source": payload.get("file_path", payload.get("slug", "")),
            "excerpt": payload.get("text", ""),
        })
    return hits
