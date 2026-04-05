"""Top-level pipeline orchestration for carta embed."""

import concurrent.futures
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
from carta.embed.parse import extract_pdf_text, extract_markdown_text, chunk_text
from carta.embed.embed import ensure_collection, upsert_chunks, get_embedding, upsert_visual_pages
from carta.embed.induct import generate_sidecar_stub, read_sidecar, write_sidecar
from carta.embed.lifecycle import needs_rehash, compute_file_hash, mark_sidecar_stale, check_stale_alert


_SUPPORTED_EXTENSIONS = [".pdf", ".md"]

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


def discover_stale_files(repo_root: Path) -> list[Path]:
    """Find all files with .embed-meta.yaml sidecars marked status: stale under repo_root.

    Returns list of file paths (the document path, not the sidecar path).
    """
    results = []
    for sidecar_path in repo_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sidecar_path)
        if data is None:
            continue
        if data.get("status") != "stale":
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
            results.append(source_file)

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
    progress=None,
) -> tuple[int, dict]:
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
    if progress:
        progress.step(f"extracting {file_path.suffix} text")
    elif verbose:
        print(f"    extracting {file_path.suffix} text...", flush=True)
    if file_path.suffix == ".md":
        pages, frontmatter_meta = extract_markdown_text(file_path)
    else:
        pages = extract_pdf_text(file_path)
        frontmatter_meta = {}
    if progress:
        progress.step(f"chunking {len(pages)} page(s)")
    elif verbose:
        print(f"    extracted {len(pages)} page(s); chunking...", flush=True)
    raw_chunks = chunk_text(pages, max_tokens=max_tokens, overlap_fraction=overlap_fraction)
    if progress:
        progress.step(f"embedding {len(raw_chunks)} chunks → Qdrant")
    elif verbose:
        print(f"    built {len(raw_chunks)} chunk(s); embedding + upserting...", flush=True)

    slug = file_info.get("slug", file_path.stem)
    metadata = {
        "slug": slug,
        "file_path": str(file_path.relative_to(repo_root)),
        "doc_type": file_info.get("doc_type", "unknown"),
    }
    if frontmatter_meta:
        metadata["frontmatter"] = frontmatter_meta

    enriched = [{**metadata, **chunk} for chunk in raw_chunks]
    count = upsert_chunks(enriched, cfg, client=client)

    # Vision: extract image descriptions for PDF files (fail-open per D-11, D-12)
    image_count = 0
    image_chunk_count = 0
    vision_metadata = None
    visual_pages_count = 0  # NEW: Count of pages embedded via ColPali

    if file_path.suffix == ".pdf":
        # Check if ColPali multimodal embedding is enabled (Issue #1)
        colpali_enabled = cfg.get("embed", {}).get("colpali_enabled", False)

        # NEW: ColPali multimodal path for visual pages
        if colpali_enabled:
            if progress:
                progress.step("ColPali: embedding visual pages")
            try:
                visual_pages_count = _embed_visual_pages_colpali(
                    file_path, file_info, cfg, client, repo_root, verbose
                )
            except Exception as exc:
                # Fail-open: log error but continue with standard extraction
                print(
                    f"Warning: ColPali visual embedding failed for {file_path}: {exc}",
                    file=sys.stderr,
                    flush=True
                )

        # Use intelligent extraction with GLM-OCR/LLaVA routing (Phase 999.4)
        if progress:
            progress.step("extracting image descriptions")
        try:
            from carta.vision.router import extract_image_descriptions_intelligent
            img_descs = extract_image_descriptions_intelligent(file_path, cfg)
            image_count = len(img_descs)

            if img_descs:
                image_chunks = []
                for desc in img_descs:
                    image_chunks.append({
                        "slug": slug,
                        "file_path": str(file_path.relative_to(repo_root)),
                        "doc_type": "image_description",
                        "page_num": desc["page_num"],
                        "image_index": desc["image_index"],
                        "chunk_index": len(raw_chunks) + len(image_chunks),
                        "text": desc["text"],
                        # Phase 999.4: extraction provenance
                        "model_used": desc.get("model_used", "llava"),
                        "content_type": desc.get("content_type", "visual"),
                    })
                image_chunk_count = upsert_chunks(image_chunks, cfg, client=client)
                if verbose:
                    print(f"    embedded {image_chunk_count} image description chunk(s)", flush=True)

                # Build vision metadata for sidecar (Phase 999.4-04)
                vision_metadata = _build_vision_metadata(img_descs)
        except Exception as exc:
            # Fail-open: log error but don't block embedding
            print(
                f"Warning: intelligent vision extraction failed for {file_path}: {exc}",
                file=sys.stderr,
                flush=True
            )
            # Fallback to legacy extraction if available
            try:
                from carta.embed.vision import extract_image_descriptions
                img_descs = extract_image_descriptions(file_path, cfg)
                image_count = len(img_descs)

                if img_descs:
                    image_chunks = []
                    for desc in img_descs:
                        image_chunks.append({
                            "slug": slug,
                            "file_path": str(file_path.relative_to(repo_root)),
                            "doc_type": "image_description",
                            "page_num": desc["page_num"],
                            "image_index": desc["image_index"],
                            "chunk_index": len(raw_chunks) + len(image_chunks),
                            "text": desc["text"],
                        })
                    image_chunk_count = upsert_chunks(image_chunks, cfg, client=client)
                    if verbose:
                        print(f"    embedded {image_chunk_count} image description chunk(s) (legacy)", flush=True)
            except Exception as legacy_exc:
                print(
                    f"Warning: legacy vision extraction also failed: {legacy_exc}",
                    file=sys.stderr,
                    flush=True
                )

    sidecar_updates = {
        "status": "embedded",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "chunk_count": count + image_chunk_count,
        "image_count": image_count,
        "image_chunks": image_chunk_count,
        "file_mtime": os.path.getmtime(str(file_path)),
        "visual_pages": visual_pages_count,  # NEW: ColPali visual pages
    }
    
    # Add vision metadata if available (Phase 999.4-04)
    if vision_metadata:
        sidecar_updates["vision"] = vision_metadata
        
    # Add ColPali metadata if visual pages were embedded (Issue #1)
    if visual_pages_count > 0:
        sidecar_updates["colpali"] = {
            "enabled": True,
            "visual_pages_embedded": visual_pages_count,
        }
    
    return count + image_chunk_count, sidecar_updates


def _build_vision_metadata(img_descs: list[dict]) -> dict:
    """Build vision metadata dict for sidecar from extraction results.
    
    Args:
        img_descs: List of extraction result dicts from intelligent routing
        
    Returns:
        Vision metadata dict for sidecar
    """
    # Count pages by model used
    glm_ocr_pages = sum(1 for d in img_descs if d.get("model_used") == "glm-ocr")
    llava_pages = sum(1 for d in img_descs if d.get("model_used") == "llava")
    hybrid_pages = sum(1 for d in img_descs if d.get("model_used") == "hybrid")
    
    # Build per-page details
    page_details = []
    for desc in img_descs:
        page_details.append({
            "page": desc.get("page_num", 0),
            "content_type": desc.get("content_type", "visual"),
            "model": desc.get("model_used", "llava"),
            "has_tables": desc.get("has_tables", False),
            "confidence": desc.get("confidence", 0.0),
        })
    
    return {
        "enabled": True,
        "pages_analyzed": len(img_descs),
        "extraction_summary": {
            "glm_ocr_pages": glm_ocr_pages,
            "llava_pages": llava_pages,
            "hybrid_pages": hybrid_pages,
        },
        "page_details": page_details,
    }


def _embed_visual_pages_colpali(
    file_path: Path,
    file_info: dict,
    cfg: dict,
    client,
    repo_root: Path,
    verbose: bool = False,
) -> int:
    """Embed visual PDF pages using ColPali/ColQwen2 late-interaction retrieval.

    This function implements the parallel multimodal embedding pathway (Issue #1).
    It embeds each page as multi-vector patches and stores the page PNG in cache.

    Args:
        file_path: Absolute path to the PDF file.
        file_info: Sidecar data dict (slug, doc_type, etc.).
        cfg: Carta config dict (must contain colpali_* settings).
        client: Connected QdrantClient.
        repo_root: Repo root for relative path computation.
        verbose: If True, print progress to stdout.

    Returns:
        Number of visual pages embedded.

    Raises:
        ImportError: If colpali-engine is not installed.
        ColPaliError: If embedding fails.
    """
    # Check if ColPali is available
    from carta.embed.colpali import is_colpali_available, ColPaliEmbedder

    if not is_colpali_available():
        if verbose:
            print(
                "    ColPali not available (install with: pip install 'carta-cc[visual'])",
                flush=True,
            )
        return 0

    # Get ColPali config
    embed_cfg = cfg.get("embed", {})
    model_name = embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0")
    device = embed_cfg.get("colpali_device", "cpu")
    batch_size = embed_cfg.get("colpali_batch_size", 1)
    cache_dir = embed_cfg.get("colpali_sidecar_path", ".carta/visual_cache/")
    
    # Ensure cache_dir is absolute (relative to repo_root)
    cache_dir_path = Path(cache_dir)
    if not cache_dir_path.is_absolute():
        cache_dir_path = repo_root / cache_dir_path
    cache_dir = str(cache_dir_path)

    # Get file slug
    slug = file_info.get("slug", file_path.stem)

    if verbose:
        print(f"    ColPali: embedding visual pages with {model_name}...", flush=True)

    try:
        # Initialize embedder
        embedder = ColPaliEmbedder(
            model_name=model_name,
            device=device,
            batch_size=batch_size,
            cache_dir=cache_dir,
        )

        # Determine which pages to embed based on visual content
        # For now, embed all pages (intelligent routing can be added later)
        # TODO: Use page classifier to select only visual-rich pages

        # Embed all pages (or use specific page numbers if classified)
        page_results = embedder.embed_pdf_pages(file_path, page_nums=None)

        if not page_results:
            return 0

        # Save PNGs to cache and prepare for Qdrant upsert
        visual_pages = []
        for result in page_results:
            page_num = result["page_num"]
            vectors = result["vectors"]
            png_bytes = result["png_bytes"]

            # Save PNG to cache
            png_path = embedder.save_page_cache(file_path, page_num, png_bytes)

            # Prepare visual page metadata for Qdrant
            # Handle case where cache_dir is not inside repo_root
            try:
                png_rel_path = str(png_path.relative_to(repo_root))
            except ValueError:
                # png_path is outside repo_root, use absolute path
                png_rel_path = str(png_path)

            # Prepare visual page metadata for Qdrant
            visual_pages.append({
                "slug": slug,
                "file_path": str(file_path.relative_to(repo_root)),
                "page_num": page_num,
                "vectors": vectors,
                "png_path": png_rel_path,
                "doc_type": "visual_page",
                "extraction_model": model_name,
            })

        # Upsert to visual collection
        if visual_pages:
            upserted = upsert_visual_pages(visual_pages, cfg, client=client)
            if verbose:
                print(f"    ColPali: embedded {upserted} visual page(s)", flush=True)
            return upserted

        return 0

    except Exception as exc:
        print(
            f"Warning: ColPali embedding failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise


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


def run_embed_file(path: Path, cfg: dict, force: bool = False, verbose: bool = False, progress=None) -> dict:
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

    # Generate sidecar if it doesn't exist
    if not sidecar_path.exists():
        stub = generate_sidecar_stub(file_path, repo_root, cfg)
        write_sidecar(file_path, stub)

    # Read sidecar for file_info
    sidecar_data = read_sidecar(sidecar_path) or {}

    # Mtime fast-path: skip hash computation if mtime unchanged (unless force=True)
    if not force and not needs_rehash(file_path, sidecar_data):
        return {"status": "skipped", "reason": "already embedded, file unchanged"}

    # Hash comparison: check if content has changed
    current_hash = compute_file_hash(file_path)
    old_hash = sidecar_data.get("file_hash")
    current_mtime = os.path.getmtime(str(file_path))

    if current_hash == old_hash and old_hash is not None:
        # Hash unchanged: just update mtime and fast-path fields
        _update_sidecar(sidecar_path, {
            "file_mtime": current_mtime,
            "last_hash_check_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": "skipped", "reason": "already embedded, file hash unchanged"}

    # Hash changed: mark for re-embedding and update lifecycle fields
    now = datetime.now(timezone.utc)
    old_generation = sidecar_data.get("generation", 0)
    new_generation = old_generation + 1

    # Build version_history entry
    version_entry = {
        "hash": current_hash,
        "generation": new_generation,
        "indexed_at": now.isoformat(),
    }

    # Get current version_history and append new entry
    version_history = sidecar_data.get("version_history", [])
    version_history.append(version_entry)

    # Trim to max_generations
    max_gens = cfg.get("embed", {}).get("max_generations", 2)
    if len(version_history) > max_gens:
        version_history = version_history[-max_gens:]

    # Prepare lifecycle updates
    lifecycle_updates = {
        "generation": new_generation,
        "status": "stale",
        "stale_as_of": now.isoformat(),
        "file_hash": current_hash,
        "file_mtime": current_mtime,
        "last_hash_check_at": now.isoformat(),
        "version_history": version_history,
    }

    # Mark chunks as stale in Qdrant (with migration boundary guard)
    if sidecar_data.get("sidecar_id"):
        client = QdrantClient(url=cfg["qdrant_url"], timeout=5)
        mark_sidecar_stale(client, collection_name(cfg, "doc"), sidecar_data.get("sidecar_id"), now)

    # Proceed with re-embedding
    file_info = {
        "slug": sidecar_data.get("slug", file_path.stem),
        "doc_type": sidecar_data.get("doc_type", "unknown"),
        "sidecar_path": sidecar_path,
        "file_path": file_path,
    }

    client = QdrantClient(url=cfg["qdrant_url"], timeout=5)
    ensure_collection(client, collection_name(cfg, "doc"))

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 400)
    overlap_fraction = chunking.get("overlap_fraction", 0.15)

    count, sidecar_updates = _embed_one_file(
        file_path, file_info, cfg, client, repo_root, max_tokens, overlap_fraction, verbose, progress
    )
    # Merge lifecycle updates with embedding updates
    sidecar_updates.update(lifecycle_updates)
    _update_sidecar(sidecar_path, sidecar_updates)
    return {"status": "ok", "chunks": count}


def run_embed(repo_root: Path, cfg: dict, verbose: bool = False, progress=None) -> dict:
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

    # Auto-induct any supported files that lack a sidecar (e.g. after sidecar deletion)
    docs_root_path = repo_root / cfg.get("docs_root", "docs/")
    if docs_root_path.is_dir():
        for ext in _SUPPORTED_EXTENSIONS:
            for file_path in docs_root_path.rglob(f"*{ext}"):
                sidecar_path = file_path.parent / (file_path.stem + ".embed-meta.yaml")
                if not sidecar_path.exists():
                    stub = generate_sidecar_stub(file_path, repo_root, cfg)
                    write_sidecar(file_path, stub)
                    if verbose:
                        print(f"  inducted: {file_path.relative_to(repo_root)}", flush=True)

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 400)
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
            if progress:
                progress.file(idx, file_path.name)
                progress.skip("LFS pointer")
            elif verbose:
                print(f"  [{idx}/{total}] SKIP (LFS pointer): {file_path.name}", flush=True)
            summary["skipped"] += 1
            continue

        if progress:
            progress.file(idx, file_path.name)
        elif verbose:
            print(f"  [{idx}/{total}] Embedding: {file_path.name} ...", flush=True)
        t0 = time.monotonic()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _embed_one_file,
                file_path, file_info, cfg, client, repo_root,
                max_tokens, overlap_fraction, verbose, progress,
            )
            try:
                count, sidecar_updates = future.result(timeout=FILE_TIMEOUT_S)
                _update_sidecar(sidecar_path, sidecar_updates)
                elapsed = time.monotonic() - t0
                if progress:
                    progress.done(chunks=count, elapsed=elapsed)
                elif verbose:
                    print(f"  [{idx}/{total}] OK: {file_path.name} — {count} chunk(s) in {elapsed:.1f}s", flush=True)
                summary["embedded"] += 1
            except concurrent.futures.TimeoutError:
                if progress:
                    progress.skip(f"timeout after {FILE_TIMEOUT_S}s")
                elif verbose:
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
                if progress:
                    progress.error(str(e))
                print(
                    f"  [{idx}/{total}] ERROR: {file_path.name} ({elapsed:.1f}s): {e}",
                    file=sys.stderr, flush=True,
                )
                summary["errors"].append(f"Error processing {file_path.name}: {e}")

    # Emit stale alert after embed loop
    stale_count = summary["embedded"]  # Files with status="stale" are those that had content changes
    total_count = summary["embedded"] + summary["skipped"]
    threshold = cfg.get("embed", {}).get("stale_alert_threshold", 0.30)
    alert_msg = check_stale_alert(stale_count, total_count, threshold)
    if alert_msg:
        print(alert_msg, flush=True)

    return summary


def run_search(query: str, cfg: dict, verbose: bool = False) -> list[dict]:
    """Search both text and visual collections for results matching query.

    Args:
        query: natural-language search query.
        cfg: carta config dict.
        verbose: unused, kept for interface consistency.

    Returns:
        List of dicts: {"score": float, "source": str, "excerpt": str}
        Ordered by descending similarity score.
    """
    from carta.search.scoped import get_search_collections
    from qdrant_client import QdrantClient
    from pathlib import Path
    
    top_n = cfg.get("search", {}).get("top_n", 5)
    repo_root = Path(find_config()).parent
    
    # Get all collections to search
    try:
        collections = get_search_collections(cfg, "repo")
    except ValueError:
        # Fall back to default collections
        collections = [collection_name(cfg, "doc")]
        if cfg.get("embed", {}).get("colpali_enabled", False):
            collections.append(f"{cfg['project_name']}_visual")
    
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e
    
    # Search across all collections and merge results
    all_results = []
    
    for coll_name in collections:
        try:
            if coll_name.endswith("_visual"):
                # Visual collection search using ColPali
                from carta.embed.colpali import is_colpali_available, ColPaliEmbedder, ColPaliError
                
                if not is_colpali_available():
                    continue
                    
                embed_cfg = cfg.get("embed", {})
                if not embed_cfg.get("colpali_enabled", False):
                    continue
                    
                model_name = embed_cfg.get("colpali_model", "vidore/colpali-v1.3-hf")
                device = embed_cfg.get("colpali_device", "cpu")
                
                try:
                    embedder = ColPaliEmbedder(
                        model_name=model_name,
                        device=device,
                        batch_size=1,
                    )
                    query_vectors = embedder.embed_query(query)
                    query_vector_list = query_vectors.tolist() if hasattr(query_vectors, "tolist") else list(query_vectors)
                    
                    response = client.query_points(
                        collection_name=coll_name,
                        query=query_vector_list,
                        using="colpali",
                        limit=top_n,
                        with_payload=True,
                    )
                    
                    for r in response.points:
                        payload = r.payload or {}
                        all_results.append({
                            "score": r.score,
                            "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                            "excerpt": f"[Visual result] Page {payload.get('page_num', '?')} - {payload.get('file_path', '')}",
                            "type": "visual",
                        })
                        
                except Exception:
                    # Skip visual search on error
                    pass
            else:
                # Text collection search using standard embeddings
                ollama_url = cfg["embed"]["ollama_url"]
                model = cfg["embed"]["ollama_model"]
                query_vec = get_embedding(query, ollama_url=ollama_url, model=model, prefix="search_query: ")
                
                response = client.query_points(
                    collection_name=coll_name,
                    query=query_vec,
                    limit=top_n,
                    with_payload=True,
                )
                
                for r in response.points:
                    payload = r.payload or {}
                    all_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""),
                        "type": "text",
                    })
        except Exception:
            # Skip collections that fail
            pass
    
    # Sort by score descending and take top_n
    all_results.sort(key=lambda x: x["score"], reverse=True)
    return all_results[:top_n]
