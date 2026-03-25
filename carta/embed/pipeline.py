"""Top-level pipeline orchestration for carta embed."""

import concurrent.futures
import sys
import time
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

# Maximum seconds to allow a single file's embed processing to run
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
    verbose: bool,
) -> tuple[int, dict]:
    """Extract, chunk, embed and upsert one file. Returns (chunk_count, sidecar_updates)."""
    if verbose:
        print(f"    extracting PDF text...", flush=True)
    pages = extract_pdf_text(file_path)
    if verbose:
        print(f"    extracted {len(pages)} page(s); chunking...", flush=True)
    raw_chunks = chunk_text(pages, max_tokens=max_tokens, overlap_fraction=overlap_fraction)
    if verbose:
        print(f"    built {len(raw_chunks)} chunk(s); embedding + upserting...", flush=True)

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
    }
    return count, sidecar_updates


def _heal_sidecar_current_paths(repo_root: Path, verbose: bool = False) -> int:
    """Add current_path to sidecars that are missing the field.

    Skips sidecars whose source file does not exist.

    Args:
        repo_root: root directory to scan for .embed-meta.yaml files.
        verbose: if True, print a summary of healed sidecars.

    Returns:
        Number of sidecars healed.
    """
    healed = 0
    for sidecar_path in repo_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sidecar_path)
        if data is None or "current_path" in data:
            continue
        stem = sidecar_path.name.replace(".embed-meta.yaml", "")
        for ext in _SUPPORTED_EXTENSIONS:
            candidate = sidecar_path.parent / f"{stem}{ext}"
            if candidate.exists():
                data["current_path"] = str(candidate.relative_to(repo_root))
                _update_sidecar(sidecar_path, data)
                healed += 1
                break
    if verbose and healed:
        print(f"carta embed: healed {healed} sidecar(s) missing current_path", flush=True)
    return healed


def run_embed(repo_root: Path, cfg: dict, verbose: bool = False) -> dict:
    """Run the embed pipeline on all pending files under repo_root.

    Args:
        repo_root: root directory to scan for .embed-meta.yaml sidecars.
        cfg: carta config dict.
        verbose: if True, print progress to stdout. If False, stdout is silent.

    Returns:
        {"embedded": int, "skipped": int, "errors": list[str]}
    """
    summary: dict = {"embedded": 0, "skipped": 0, "errors": []}

    # Pre-flight: check Qdrant reachability with a short timeout
    if verbose:
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

    # Heal sidecars missing current_path before processing
    _heal_sidecar_current_paths(repo_root, verbose=verbose)

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 800)
    overlap_fraction = chunking.get("overlap_fraction", 0.15)

    pending = discover_pending_files(repo_root)
    total = len(pending)
    if verbose:
        print(f"carta embed: {total} file(s) pending.", flush=True)

    for idx, file_info in enumerate(pending, start=1):
        file_path: Path = file_info["file_path"]
        sidecar_path: Path = file_info["sidecar_path"]

        # LFS guard
        if is_lfs_pointer(file_path):
            if verbose:
                print(f"  [{idx}/{total}] SKIP (LFS pointer): {file_path.name}", flush=True)
            summary["skipped"] += 1
            continue

        if verbose:
            print(f"  [{idx}/{total}] Embedding: {file_path.name} ...", flush=True)
        t0 = time.monotonic()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _embed_one_file,
                file_path, file_info, cfg, client, repo_root,
                max_tokens, overlap_fraction, verbose,
            )
            try:
                count, sidecar_updates = future.result(timeout=FILE_TIMEOUT_S)
                _update_sidecar(sidecar_path, sidecar_updates)
                elapsed = time.monotonic() - t0
                if verbose:
                    print(f"  [{idx}/{total}] OK: {file_path.name} — {count} chunk(s) in {elapsed:.1f}s", flush=True)
                summary["embedded"] += 1
            except concurrent.futures.TimeoutError:
                if verbose:
                    print(
                        f"  [{idx}/{total}] TIMEOUT: {file_path.name} exceeded {FILE_TIMEOUT_S}s -- skipping",
                        flush=True,
                    )
                print(
                    f"  TIMEOUT: {file_path.name} exceeded {FILE_TIMEOUT_S}s",
                    file=sys.stderr, flush=True,
                )
                summary["skipped"] += 1
            except Exception as e:
                elapsed = time.monotonic() - t0
                print(
                    f"  [{idx}/{total}] ERROR: {file_path.name} ({elapsed:.1f}s): {e}",
                    file=sys.stderr, flush=True,
                )
                summary["errors"].append(f"Error processing {file_path.name}: {e}")

    return summary


def run_search(query: str, cfg: dict, verbose: bool = False) -> list[dict]:
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
