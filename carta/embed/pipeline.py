"""Top-level pipeline orchestration for carta embed."""

import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Optional

import yaml
from qdrant_client import QdrantClient
from qdrant_client import models as qmodels
from qdrant_client.models import Filter

from carta import __version__ as _CARTA_VERSION
from carta.config import collection_name, find_config
from carta.embed.parse import extract_pdf_text, extract_pdf_text_and_classify, extract_markdown_text, chunk_text, _estimate_tokens
from carta.embed.embed import (
    ensure_collection,
    upsert_chunks,
    get_embedding,
    upsert_visual_pages,
    collection_is_hybrid,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
)
from carta.embed.sparse import embed_sparse_query
from carta.embed.induct import generate_sidecar_stub, read_sidecar, write_sidecar, sidecar_path
from carta.embed.lifecycle import needs_rehash, compute_file_hash, mark_sidecar_stale, check_stale_alert
from carta.embed.visual_queue import add_pending_pages, move_to_done, VISUAL_PENDING_KEY, VISUAL_DONE_KEY, queue_summary, format_summary_line
from carta.embed.colpali import is_colpali_available
from carta.embed.status import StatusWriter
from carta.vision.classifier import PageClass, PageAnalyzer

_IMAGE_HEAVY = {PageClass.TEXT_WITH_IMAGES, PageClass.FLATTENED}


def _mark_or_collect_visual_pages(page_classes: list, cfg: dict) -> dict:
    """Return sidecar updates queuing 1-indexed image-heavy pages when two_pass_visual is on.

    Args:
        page_classes: List of PageClass values, one per page (0-indexed position = page-1).
        cfg: Carta config dict. Reads embed.two_pass_visual.

    Returns:
        Dict with VISUAL_PENDING_KEY → sorted list of 1-indexed page numbers, or {} when
        two_pass_visual is off or no image-heavy pages exist.
    """
    if not cfg.get("embed", {}).get("two_pass_visual", True):
        return {}
    pending = [i + 1 for i, pc in enumerate(page_classes) if pc in _IMAGE_HEAVY]
    updates: dict = {}
    if pending:
        add_pending_pages(updates, pending)  # writes updates[VISUAL_PENDING_KEY]
    return updates


_SUPPORTED_EXTENSIONS = [".pdf", ".md"]

# Maximum seconds to allow a single file's embed processing to run
FILE_TIMEOUT_S = 300

# Env var that, when set, points run_embed at a JSONL log it appends one row
# to per processed file. Useful for profiling long batch runs.
_PERF_LOG_ENV = "CARTA_PERF_LOG"


def _resolve_perf_log_path(repo_root: Path) -> Optional[Path]:
    """Resolve CARTA_PERF_LOG to an absolute path, or None if unset.

    Also touches the file so `tail -f` can attach before the first row lands.
    """
    raw = os.environ.get(_PERF_LOG_ENV)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except Exception as exc:
        print(f"Warning: cannot create perf log file {path}: {exc}",
              file=sys.stderr, flush=True)
        return None
    return path


def _build_perf_context(cfg: dict) -> dict:
    """Static fields included on every perf-log row for a single embed session."""
    embed_cfg = cfg.get("embed", {}) or {}
    return {
        "carta_version": _CARTA_VERSION,
        "models": {
            "embedding": embed_cfg.get("ollama_model"),
            "vision":    embed_cfg.get("ollama_vision_model"),
            "ocr":       embed_cfg.get("ocr_model"),
        },
        "workers": {
            "vision":    int(embed_cfg.get("vision_workers", 4)),
            "embedding": int(embed_cfg.get("embedding_workers", 8)),
        },
    }


def _summarize_vision_strategies(events: list[dict]) -> dict[str, int]:
    """Count vision-router events by model_used (e.g. {'glm-ocr': 30, 'llava': 5})."""
    counts: dict[str, int] = {}
    for e in events or []:
        m = e.get("model_used", "unknown")
        counts[m] = counts.get(m, 0) + 1
    return counts


def _vision_checkpoint_path(repo_root: Path, slug: str) -> Path:
    """Path used to persist mid-PDF vision-extraction progress for resume.

    Cleared on full file completion; left in place when a run is interrupted
    (timeout, error, ^C) so the next ``carta embed`` can pick up where it
    stopped instead of redoing every already-OCR'd page.
    """
    return repo_root / ".carta" / "checkpoints" / f"{slug}.json"


def _write_perf_log_entry(path: Optional[Path], entry: dict) -> None:
    """Append one JSONL row. Logging failures must never abort the embed."""
    if path is None:
        return
    entry = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), **entry}
    try:
        with path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"Warning: perf log write failed: {exc}", file=sys.stderr, flush=True)


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
    """Find all sidecars under .carta/sidecars/ with status: pending.

    Returns list of dicts: sidecar data + 'sidecar_path' + 'file_path'.
    """
    results = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return results
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None or data.get("status") != "pending":
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue
        source_file = repo_root / current_path
        if source_file.exists():
            results.append({**data, "sidecar_path": sc_path, "file_path": source_file})
    return results


def discover_stale_files(repo_root: Path) -> list[Path]:
    """Find all files with sidecars under .carta/sidecars/ marked status: stale.

    Returns list of source file paths.
    """
    results = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return results
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None or data.get("status") != "stale":
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue
        source_file = repo_root / current_path
        if source_file.exists():
            results.append(source_file)
    return results


def _glob_scope_re(pattern: str):
    """Compile a glob pattern (possibly with **) to a full-path anchored regex.

    Rules:
    - ``**`` between slashes matches zero or more path segments.
    - ``**`` elsewhere matches anything (including slashes).
    - ``*`` matches any chars except '/'.
    - ``?`` matches any single char except '/'.
    """
    escaped = re.escape(pattern)
    # Use a placeholder so we can differentiate ** from single *
    escaped = escaped.replace(r"\*\*", "\x00DSTAR\x00")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace(r"\?", "[^/]")
    # /**/ → allow zero or more intermediate segments
    escaped = escaped.replace("/\x00DSTAR\x00/", "(?:/.*/|/)")
    # Any remaining DSTAR (leading/trailing) → match anything
    escaped = escaped.replace("\x00DSTAR\x00", ".*")
    return re.compile("^" + escaped + "$")


def _colpali_path_in_scope(rel_path: str, scopes: list[str]) -> bool:
    """Return True if rel_path should receive ColPali visual embedding.

    Empty scopes means no restriction — all paths are in scope (current behavior).

    Matching rules (applied in order; first match wins True):
    - A scope entry ending with '/' is treated as a directory prefix: any path
      that starts with that prefix is in scope.
    - All other entries are treated as a glob pattern supporting both ``*``
      (single-segment wildcard) and ``**`` (cross-segment wildcard). Matching
      is anchored to the full repo-relative path.
    """
    if not scopes:
        return True
    p = PurePath(rel_path)
    for scope in scopes:
        if scope.endswith("/"):
            # Directory-prefix match: rel_path must be inside the dir
            dir_prefix = scope.rstrip("/")
            try:
                p.relative_to(dir_prefix)
                return True
            except ValueError:
                pass
        else:
            # Glob pattern match — use regex engine for ** support
            if _glob_scope_re(scope).match(rel_path):
                return True
    return False


def _split_vision_text(text: str, max_tokens: int) -> list[str]:
    """Split oversized vision chunk text into word-window parts.

    GLM-OCR can produce very large extractions from dense table pages. Rather
    than truncating (losing data) or sending the full blob to Ollama (triggering
    retry loops), split into multiple chunks of <= max_tokens each.

    Args:
        text: The extracted vision text.
        max_tokens: Target max tokens per chunk (uses same estimate as chunk_text).

    Returns:
        List of text parts; length 1 if text fits in one chunk.
    """
    if _estimate_tokens(text) <= max_tokens:
        return [text]
    word_limit = max(1, int(max_tokens / 1.3))
    words = text.split()
    return [" ".join(words[i:i + word_limit]) for i in range(0, len(words), word_limit)]


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
    cancel_event: Optional[threading.Event] = None,
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
    # For PDFs with two_pass_visual enabled, classify pages in the SAME fitz pass as
    # text extraction — avoid opening the PDF twice.
    _page_classes_from_extraction: list | None = None  # populated for PDFs when two_pass_visual is on
    if file_path.suffix == ".md":
        pages, frontmatter_meta = extract_markdown_text(file_path)
    elif file_path.suffix == ".pdf" and cfg.get("embed", {}).get("two_pass_visual", True):
        try:
            analyzer = PageAnalyzer(cfg)
            pages, _page_classes_from_extraction = extract_pdf_text_and_classify(file_path, analyzer)
        except Exception as _cls_exc:
            # Classification failed: fall back to text-only extraction and skip inline vision
            # (fail closed — do not escalate to the heavy VLM path).
            print(
                f"Warning: two_pass_visual page classification failed for {file_path}: {_cls_exc}; "
                f"pages left unclassified — skipping inline vision for this file",
                file=sys.stderr,
                flush=True,
            )
            pages = extract_pdf_text(file_path)
            _page_classes_from_extraction = None  # signals: skip both inline vision AND two-pass queue
        frontmatter_meta = {}
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

    # Vision progress tracking — events collected by callback, passed to caller via sidecar_updates
    _page_events: list[dict] = []

    def _vision_callback(
        page_num: int,
        total_pages: int,
        page_class: str,
        model_used: str,
        char_count: int,
    ) -> None:
        _page_events.append({
            "page": page_num,
            "page_class": page_class,
            "model_used": model_used,
            "char_count": char_count,
        })
        if progress:
            if model_used == "skip":
                msg = f"vision: page {page_num}/{total_pages} → pure-text (skip)"
            else:
                msg = f"vision: page {page_num}/{total_pages} → {model_used} → {char_count} chars"
            progress.step(msg)

    # Vision: extract image descriptions for PDF files (fail-open per D-11, D-12)
    image_count = 0
    image_chunk_count = 0
    vision_metadata = None
    visual_pages_count = 0  # NEW: Count of pages embedded via ColPali
    _visual_queue_updates: dict = {}  # populated by two_pass_visual pass-1 for PDFs

    if file_path.suffix == ".pdf":
        # Two-pass visual: classify pages cheaply and queue image-heavy ones for deferred
        # visual embedding instead of running inline VLM/ColPali.
        # Classification was already performed during text extraction above (single fitz pass).
        # _page_classes_from_extraction is:
        #   - a list[PageClass]  → classification succeeded; use two-pass path
        #   - None               → two_pass_visual is off OR classification failed (fail closed:
        #                          skip inline vision; no heavy path for this file)
        two_pass_visual = cfg.get("embed", {}).get("two_pass_visual", True)
        # _skip_inline_vision: True when we must NOT fall through to ColPali/VLM.
        # Set True when two_pass_visual queued pages normally, OR when classification
        # failed (fail closed — never escalate to the heavy path on error).
        _skip_inline_vision = False

        if two_pass_visual and _page_classes_from_extraction is not None:
            _visual_queue_updates = _mark_or_collect_visual_pages(_page_classes_from_extraction, cfg)
            _skip_inline_vision = True  # two-pass queued; inline path not needed
        elif two_pass_visual and _page_classes_from_extraction is None:
            # Classification error was already logged during extraction; fail closed.
            # Do NOT run the heavy inline vision path for this file.
            _visual_queue_updates = {}
            _skip_inline_vision = True

        if two_pass_visual and _page_classes_from_extraction is not None:
            # Skip inline ColPali + VLM entirely — image-heavy pages are queued for pass-2
            if verbose and _visual_queue_updates:
                pending_pages = _visual_queue_updates.get(VISUAL_PENDING_KEY, [])
                print(
                    f"    two_pass_visual: queuing {len(pending_pages)} image-heavy page(s) "
                    f"for deferred visual embedding",
                    flush=True,
                )
        elif not two_pass_visual:
            _visual_queue_updates = {}

        # Check if ColPali multimodal embedding is enabled (Issue #1)
        colpali_enabled = (not _skip_inline_vision) and cfg.get("embed", {}).get("colpali_enabled", False)

        # NEW: ColPali multimodal path for visual pages
        if colpali_enabled:
            # Directory/glob scoping: skip ColPali for files outside configured scope
            embed_cfg = cfg.get("embed", {})
            colpali_scoped_paths: list = embed_cfg.get("colpali_scoped_paths", [])
            rel_path_str = str(file_path.relative_to(repo_root)) if file_path.is_relative_to(repo_root) else str(file_path)
            _in_scope = _colpali_path_in_scope(rel_path_str, colpali_scoped_paths)
            if not _in_scope:
                if verbose:
                    print(
                        f"    ColPali: skipping {rel_path_str} (not in colpali_scoped_paths)",
                        flush=True,
                    )
            else:
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
        # Skipped when two_pass_visual queued pages OR when classification failed (fail closed).
        if not _skip_inline_vision:
            if progress:
                progress.step("extracting image descriptions")
            checkpoint_path = _vision_checkpoint_path(repo_root, slug)
            try:
                from carta.vision.router import extract_image_descriptions_intelligent
                img_descs = extract_image_descriptions_intelligent(
                    file_path, cfg, progress_callback=_vision_callback,
                    cancel_event=cancel_event,
                    checkpoint_path=checkpoint_path,
                )
                image_count = len(img_descs)

                if img_descs:
                    image_chunks = []
                    for desc in img_descs:
                        for part_text in _split_vision_text(desc["text"], max_tokens):
                            image_chunks.append({
                                "slug": slug,
                                "file_path": str(file_path.relative_to(repo_root)),
                                "doc_type": "image_description",
                                "page_num": desc["page_num"],
                                "image_index": desc["image_index"],
                                "chunk_index": len(raw_chunks) + len(image_chunks),
                                "text": part_text,
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

    # Merge deferred visual-queue updates (two_pass_visual pass-1: visual_pending pages)
    if _visual_queue_updates:
        sidecar_updates.update(_visual_queue_updates)

    sidecar_updates["_vision_events"] = _page_events

    # File fully embedded — clear any per-page resume checkpoint.
    if file_path.suffix == ".pdf":
        cp = _vision_checkpoint_path(repo_root, slug)
        try:
            cp.unlink(missing_ok=True)
        except OSError:
            pass

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
    model_name = embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0-hf")
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


def _discover_visual_pending(repo_root: Path) -> list[tuple]:
    """Return [(sidecar_path, sidecar_dict)] for sidecars with non-empty visual_pending."""
    out = []
    sidecars_dir = repo_root / ".carta" / "sidecars"
    if not sidecars_dir.is_dir():
        return out
    for sc_path in sidecars_dir.rglob("*.embed-meta.yaml"):
        sc = read_sidecar(sc_path)
        if sc and (sc.get(VISUAL_PENDING_KEY) or []):
            out.append((sc_path, sc))
    return out


def _visual_chunk_index_pass2(page: int, i: int) -> str:
    """Return the chunk_index token used for pass-2 visual/OCR text chunks.

    Pass-2 chunks set ``chunk_index`` to this string (e.g. ``"visual:1:0"``).
    ``upsert_chunks`` then derives the Qdrant point ID via
    ``_point_id(slug, chunk_index)`` → ``md5("{slug}:visual:{page}:{i}")``.

    This namespace is structurally disjoint from pass-1 text chunks, which
    always use integer chunk_index values (e.g. ``md5("{slug}:0")``).  An
    integer can never equal the string ``"visual:{page}:{i}"``, so collision
    between pass-1 and pass-2 chunks for the same slug is impossible by
    construction.
    """
    return f"visual:{page}:{i}"


def _visual_embed_one_page(
    sidecar: dict,
    page: int,
    cfg: dict,
    client,
    repo_root: Path,
    router,
    embedder,
    verbose: bool = False,
) -> bool:
    """OCR text + ColPali for a single 1-indexed page. Raise on failure.

    (a) glm-ocr text for the page via SmartRouter → upsert_chunks (hybrid text index).
        Pass-2 chunks receive point IDs from _visual_point_id_pass2() so they are
        disjoint from pass-1 text chunks for the same slug.
    (b) ColPali for the page via ColPaliEmbedder.embed_pdf_pages(page_nums=[page])
        → upsert_visual_pages (_visual collection).

    ``router`` and ``embedder`` are constructed ONCE by run_visual_embed and
    passed in; this function must NOT re-construct them.

    NOTE: the PDF is opened twice per page (once for OCR, once for ColPali
    rasterisation) — a known inefficiency acceptable for this slow pass.

    Raises on any failure so the caller leaves the page in visual_pending.
    """
    try:
        import fitz as _fitz
    except ImportError as e:
        raise RuntimeError("PyMuPDF (fitz) is required for visual drainer") from e

    current_path = sidecar.get("current_path", "")
    file_path = repo_root / current_path
    slug = sidecar.get("slug", file_path.stem)
    embed_cfg = cfg.get("embed", {}) or {}
    chunking = cfg.get("embed", {}).get("chunking", {}) or {}
    max_tokens = chunking.get("max_tokens", 400)

    # ── (a) GLM-OCR text → hybrid text index ─────────────────────────────────
    from carta.mupdf_util import mupdf_quiet
    with mupdf_quiet():
        try:
            doc = _fitz.open(str(file_path))
        except Exception as exc:
            raise RuntimeError(f"Cannot open PDF {file_path}: {exc}") from exc
        try:
            if page < 1 or page > len(doc):
                raise RuntimeError(
                    f"Page {page} out of range (PDF has {len(doc)} pages)"
                )
            fitz_page = doc[page - 1]
            profile = router.analyzer.analyze(fitz_page)
            chunks = router._route(fitz_page, page, profile, doc)
        finally:
            doc.close()

    if chunks:
        image_chunks = []
        for chunk in chunks:
            for part_text in _split_vision_text(chunk.get("text", ""), max_tokens):
                i = len(image_chunks)
                image_chunks.append({
                    "slug": slug,
                    "file_path": current_path,
                    "doc_type": "image_description",
                    "page_num": page,
                    "image_index": chunk.get("image_index", 0),
                    # Use a pass-2-specific chunk_index token so the Qdrant point
                    # ID (derived by upsert_chunks as md5("{slug}:{chunk_index}"))
                    # is disjoint from pass-1 text chunks (which use integer indices).
                    "chunk_index": _visual_chunk_index_pass2(page, i),
                    "text": part_text,
                    "model_used": chunk.get("model_used", "glm-ocr"),
                    "content_type": chunk.get("content_type", "visual"),
                    "source": "visual_drainer",
                })
        upsert_chunks(image_chunks, cfg, client=client)
        if verbose:
            print(
                f"    visual: page {page} OCR → {len(image_chunks)} chunk(s)",
                flush=True,
            )

    # ── (b) ColPali → _visual collection ─────────────────────────────────────
    model_name = embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0-hf")
    cache_dir_raw = embed_cfg.get("colpali_sidecar_path", ".carta/visual_cache/")
    cache_dir_path = Path(cache_dir_raw)
    if not cache_dir_path.is_absolute():
        cache_dir_path = repo_root / cache_dir_path

    page_results = embedder.embed_pdf_pages(file_path, page_nums=[page])
    if page_results:
        result = page_results[0]
        png_path = embedder.save_page_cache(file_path, page, result["png_bytes"])
        try:
            png_rel = str(png_path.relative_to(repo_root))
        except ValueError:
            png_rel = str(png_path)
        visual_pages = [{
            "slug": slug,
            "file_path": current_path,
            "page_num": page,
            "vectors": result["vectors"],
            "png_path": png_rel,
            "doc_type": "visual_page",
            "extraction_model": model_name,
            "source": "visual_drainer",
        }]
        upsert_visual_pages(visual_pages, cfg, client=client)
        if verbose:
            print(f"    visual: page {page} ColPali → upserted", flush=True)

    return True


def run_visual_embed(
    repo_root: Path,
    cfg: dict,
    verbose: bool = False,
    progress=None,
) -> dict:
    """Drain the visual_pending queue: OCR text + ColPali per page, one at a time.

    Discovers every sidecar with a non-empty ``visual_pending`` list, then for
    each pending page runs (a) glm-ocr text extraction → hybrid text index and
    (b) ColPali page-image embedding → ``_visual`` collection.  Each page is
    checkpointed immediately after success (``visual_pending → visual_done``);
    a page that errors is left pending for the next run.

    Args:
        repo_root: Repo root (``Path``).
        cfg: Carta config dict.
        verbose: Print per-page progress when True.
        progress: Optional Progress UI object (unused; reserved for future TUI).

    Returns:
        Summary dict: ``{"pages_embedded": int, "pages_failed": int, "files": int}``
        When ColPali is unavailable the dict also contains ``"status": "visual_unavailable"``.
    """
    summary: dict = {"pages_embedded": 0, "pages_failed": 0, "files": 0}

    if not is_colpali_available():
        print(
            "carta embed --visual: the [visual] extra (torch+transformers) is not "
            "installed. Install with: pip install 'carta-cc[visual]'  (may require a "
            "Python 3.12 venv if torch wheels are unavailable for the current "
            "interpreter).",
            flush=True,
        )
        summary["status"] = "visual_unavailable"
        return summary

    client = QdrantClient(url=cfg["qdrant_url"], timeout=5)
    queued = _discover_visual_pending(repo_root)
    summary["files"] = len(queued)
    total_pages = sum(len(sc.get(VISUAL_PENDING_KEY, []) or []) for _, sc in queued)

    # Status-line widget: track the --visual drain too (not just `carta embed`).
    status = StatusWriter(repo_root, enabled=cfg.get("embed", {}).get("status_file", True))
    status.start(total_pages)

    # Construct router and embedder ONCE for the entire drain — not per page.
    from carta.vision.router import SmartRouter
    from carta.embed.colpali import ColPaliEmbedder
    # Pin torch to one thread: ColPali matmuls otherwise crash Apple's multithreaded
    # Accelerate BLAS on macOS (belt-and-suspenders with the env-var pin in carta._compat).
    try:
        import torch
        torch.set_num_threads(1)
    except Exception:
        pass
    embed_cfg = cfg.get("embed", {}) or {}
    model_name = embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0-hf")
    device = embed_cfg.get("colpali_device", "cpu")
    batch_size = embed_cfg.get("colpali_batch_size", 1)
    cache_dir_raw = embed_cfg.get("colpali_sidecar_path", ".carta/visual_cache/")
    cache_dir_path = Path(cache_dir_raw)
    if not cache_dir_path.is_absolute():
        cache_dir_path = repo_root / cache_dir_path
    router = SmartRouter(cfg)
    embedder = ColPaliEmbedder(
        model_name=model_name,
        device=device,
        batch_size=batch_size,
        cache_dir=str(cache_dir_path),
    )

    idx = 0
    try:
        for sc_path, sc in queued:
            for page in list(sc.get(VISUAL_PENDING_KEY, []) or []):
                idx += 1
                status.file_start(idx, f"page {page} of {sc.get('current_path', '')}")
                try:
                    _visual_embed_one_page(sc, page, cfg, client, repo_root, router, embedder, verbose)
                    move_to_done(sc, page)
                    _update_sidecar(sc_path, {
                        VISUAL_PENDING_KEY: sc[VISUAL_PENDING_KEY],
                        VISUAL_DONE_KEY: sc[VISUAL_DONE_KEY],
                    })
                    summary["pages_embedded"] += 1
                    status.file_done(embedded=1)
                except Exception as e:
                    summary["pages_failed"] += 1
                    status.file_done(errors=1)
                    print(
                        f"  visual: page {page} of {sc.get('current_path')} failed: {e} "
                        f"(left pending)",
                        flush=True,
                    )
    except BaseException:
        status.finish("failed")
        raise

    status.finish("done")
    return summary


def _heal_sidecar_current_paths(repo_root: Path, verbose: bool = False) -> int:
    """Add current_path to sidecars under .carta/sidecars/ that are missing the field."""
    healed = 0
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return healed
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None or "current_path" in data:
            continue
        # Infer source path from sidecar's mirror position
        rel_from_sidecars = sc_path.relative_to(sidecars_root)
        stem = sc_path.name.replace(".embed-meta.yaml", "")
        parent_dirs = rel_from_sidecars.parent
        for ext in _SUPPORTED_EXTENSIONS:
            candidate = repo_root / parent_dirs / f"{stem}{ext}"
            if candidate.exists():
                data["current_path"] = str(parent_dirs / f"{stem}{ext}")
                _update_sidecar(sc_path, data)
                healed += 1
                break
    if verbose and healed:
        print(f"carta embed: healed {healed} sidecar(s) missing current_path", flush=True)
    return healed


def migrate_sidecars(repo_root: Path, verbose: bool = False) -> int:
    """Move co-located *.embed-meta.yaml files to .carta/sidecars/. Returns count moved."""
    moved = 0
    carta_dir = repo_root / ".carta"
    for old_path in repo_root.rglob("*.embed-meta.yaml"):
        try:
            old_path.relative_to(carta_dir)
            continue  # already inside .carta/ — skip
        except ValueError:
            pass
        rel = old_path.relative_to(repo_root)
        new_path = repo_root / ".carta" / "sidecars" / rel
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if new_path.exists():
            # Canonical sidecar already present — discard stale co-located copy
            old_path.unlink()
            moved += 1
            continue
        shutil.move(str(old_path), str(new_path))
        if verbose:
            print(f"  migrated: {rel} → {new_path.relative_to(repo_root)}", flush=True)
        moved += 1
    return moved


def detect_orphaned_sidecars(repo_root: Path) -> list[Path]:
    """Return sidecar paths under .carta/sidecars/ whose current_path source no longer exists."""
    orphans = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return orphans
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None:
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue  # skip pre-lifecycle sidecars without current_path
        if not (repo_root / current_path).exists():
            orphans.append(sc_path)
    return orphans


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

    sc_path = sidecar_path(file_path, repo_root)

    # Generate sidecar if it doesn't exist
    if not sc_path.exists():
        stub = generate_sidecar_stub(file_path, repo_root, cfg)
        write_sidecar(file_path, stub, repo_root)

    # Read sidecar for file_info
    sidecar_data = read_sidecar(sc_path) or {}

    # Mtime fast-path: skip hash computation if mtime unchanged (unless force=True)
    if not force and not needs_rehash(file_path, sidecar_data):
        return {"status": "skipped", "reason": "already embedded, file unchanged"}

    # Hash comparison: check if content has changed
    current_hash = compute_file_hash(file_path)
    old_hash = sidecar_data.get("file_hash")
    current_mtime = os.path.getmtime(str(file_path))

    if current_hash == old_hash and old_hash is not None:
        # Hash unchanged: just update mtime and fast-path fields
        _update_sidecar(sc_path, {
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
        "sidecar_path": sc_path,
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
    sidecar_updates.pop("_vision_events", None)  # temp key — never written to sidecar
    _update_sidecar(sc_path, sidecar_updates)
    return {"status": "ok", "chunks": count}


def run_embed(repo_root: Path, cfg: dict, verbose: bool = False, progress=None) -> dict:
    """Run the embed pipeline on all pending files under repo_root.

    Args:
        repo_root: root directory to scan for .embed-meta.yaml sidecars.
        cfg: carta config dict.
        verbose: if True, print progress to stdout. If False, stdout is silent.

    Returns:
        {"embedded": int, "skipped": int, "errors": list[str], "timed_out": list[str]}
    """
    summary: dict = {"embedded": 0, "skipped": 0, "errors": [], "timed_out": []}

    # Migrate any co-located sidecars from old format to .carta/sidecars/
    migrate_sidecars(repo_root, verbose=verbose)

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

    # Warn about sidecars whose source files no longer exist
    for orphan in detect_orphaned_sidecars(repo_root):
        orphan_data = read_sidecar(orphan) or {}
        print(
            f"Warning: orphaned sidecar (source not found): {orphan.relative_to(repo_root)}\n"
            f"  → source was: {orphan_data.get('current_path', 'unknown')}\n"
            f"  Run 'carta audit' for full orphan report.",
            file=sys.stderr, flush=True,
        )

    # Auto-induct any supported files that lack a sidecar (e.g. after sidecar deletion)
    docs_root_path = repo_root / cfg.get("docs_root", "docs/")
    if docs_root_path.is_dir():
        for ext in _SUPPORTED_EXTENSIONS:
            for file_path in docs_root_path.rglob(f"*{ext}"):
                sc_path = sidecar_path(file_path, repo_root)
                if not sc_path.exists():
                    stub = generate_sidecar_stub(file_path, repo_root, cfg)
                    write_sidecar(file_path, stub, repo_root)
                    if verbose:
                        print(f"  inducted: {file_path.relative_to(repo_root)}", flush=True)

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 400)
    overlap_fraction = chunking.get("overlap_fraction", 0.15)
    file_timeout_s = cfg.get("embed", {}).get("file_timeout_s", FILE_TIMEOUT_S)

    perf_log_path = _resolve_perf_log_path(repo_root)
    perf_context = _build_perf_context(cfg)

    status = StatusWriter(
        repo_root, enabled=cfg.get("embed", {}).get("status_file", True)
    )

    pending = discover_pending_files(repo_root)
    total = len(pending)
    if progress is not None:
        progress.set_total(total)
    if verbose:
        print(f"carta embed: {total} file(s) pending.", flush=True)
    status.start(total)

    try:
        for idx, file_info in enumerate(pending, start=1):
            file_path: Path = file_info["file_path"]
            sc_path: Path = file_info["sidecar_path"]
            rel_file = str(file_path.relative_to(repo_root)) if file_path.is_relative_to(repo_root) else str(file_path)

            status.file_start(idx, file_path.name)

            # LFS guard
            if is_lfs_pointer(file_path):
                if progress:
                    progress.file(idx, file_path.name)
                    progress.skip("LFS pointer")
                elif verbose:
                    print(f"  [{idx}/{total}] SKIP (LFS pointer): {file_path.name}", flush=True)
                summary["skipped"] += 1
                _write_perf_log_entry(perf_log_path, {
                    **perf_context, "file": rel_file, "status": "skip",
                    "skip_reason": "lfs_pointer", "chunks": 0, "elapsed_s": 0.0,
                })
                status.file_done(skipped=1)
                continue

            if progress:
                progress.file(idx, file_path.name)
            elif verbose:
                print(f"  [{idx}/{total}] Embedding: {file_path.name} ...", flush=True)
            t0 = time.monotonic()

            # Per-file watchdog: a daemon thread runs the work; main thread waits
            # up to file_timeout_s. Daemon thread dies cleanly at interpreter exit,
            # avoiding the "cannot schedule new futures after interpreter shutdown"
            # races a ThreadPoolExecutor-based watchdog left behind on timeout.
            cancel_event = threading.Event()
            result_holder: dict = {}

            def _worker():
                try:
                    result_holder["value"] = _embed_one_file(
                        file_path, file_info, cfg, client, repo_root,
                        max_tokens, overlap_fraction, verbose, progress,
                        cancel_event,
                    )
                except BaseException as exc:
                    result_holder["error"] = exc

            worker = threading.Thread(
                target=_worker, daemon=True, name=f"carta-embed-{file_path.name}"
            )
            worker.start()
            worker.join(timeout=file_timeout_s)
            elapsed = time.monotonic() - t0

            if worker.is_alive():
                # Timeout — signal cancel and move on. Daemon thread will be killed
                # at process exit; we do not block here.
                cancel_event.set()
                if progress:
                    progress.skip(f"timeout after {file_timeout_s}s")
                elif verbose:
                    print(
                        f"  [{idx}/{total}] TIMEOUT: {file_path.name} exceeded {file_timeout_s}s -- skipping",
                        flush=True,
                    )
                print(
                    f"  TIMEOUT: {file_path.name} exceeded {file_timeout_s}s",
                    file=sys.stderr, flush=True,
                )
                summary["skipped"] += 1
                summary["timed_out"].append(file_path.name)
                _write_perf_log_entry(perf_log_path, {
                    **perf_context, "file": rel_file, "status": "timeout",
                    "chunks": 0, "elapsed_s": round(elapsed, 2),
                    "timeout_s": file_timeout_s,
                })
                status.file_done(skipped=1)
            elif "error" in result_holder:
                exc = result_holder["error"]
                if progress:
                    progress.error(str(exc))
                print(
                    f"  [{idx}/{total}] ERROR: {file_path.name} ({elapsed:.1f}s): {exc}",
                    file=sys.stderr, flush=True,
                )
                summary["errors"].append(f"Error processing {file_path.name}: {exc}")
                _write_perf_log_entry(perf_log_path, {
                    **perf_context, "file": rel_file, "status": "error",
                    "chunks": 0, "elapsed_s": round(elapsed, 2),
                    "error": str(exc)[:200],
                })
                status.file_done(errors=1)
            else:
                count, sidecar_updates = result_holder["value"]
                vision_events = sidecar_updates.pop("_vision_events", [])
                _update_sidecar(sc_path, sidecar_updates)
                if progress:
                    progress.done(chunks=count, elapsed=elapsed)
                    if vision_events:
                        progress.vision_done(vision_events)
                elif verbose:
                    print(f"  [{idx}/{total}] OK: {file_path.name} — {count} chunk(s) in {elapsed:.1f}s", flush=True)
                summary["embedded"] += 1
                _write_perf_log_entry(perf_log_path, {
                    **perf_context, "file": rel_file, "status": "ok",
                    "chunks": count, "elapsed_s": round(elapsed, 2),
                    "vision_strategies": _summarize_vision_strategies(vision_events),
                })
                status.file_done(embedded=1, chunks=count)
    except BaseException:
        status.finish("failed")
        raise

    # Emit stale alert after embed loop
    stale_count = len(discover_stale_files(repo_root))
    total_count = summary["embedded"] + summary["skipped"] + stale_count
    threshold = cfg.get("embed", {}).get("stale_alert_threshold", 0.30)
    alert_msg = check_stale_alert(stale_count, total_count, threshold)
    if alert_msg:
        print(alert_msg, flush=True)

    # Emit visual-queue nudge: scan all sidecars for pending visual pages (pass-1 → pass-2)
    all_sidecar_dicts = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if sidecars_root.exists():
        for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
            data = read_sidecar(sc_path)
            if data is not None:
                all_sidecar_dicts.append(data)
    vq_summary = queue_summary(all_sidecar_dicts)
    vq_line = format_summary_line(vq_summary)
    if vq_line:
        print(vq_line, flush=True)
    summary["visual_queue"] = vq_summary

    status.finish("done")
    return summary


def _hybrid_query_collection(client, coll_name, query, dense_vec, top_n,
                              prefetch_limit, bm25_model):
    """Run a hybrid BM25+dense query with Qdrant RRF fusion.

    Fetches `prefetch_limit` candidates from each of the dense and sparse
    indexes, then fuses them with Reciprocal Rank Fusion and returns the
    top `top_n` results.

    `top_n` controls the Qdrant fusion `limit` (i.e. how many fused results
    to return).  When reranking is enabled, callers should pass `fetch_limit`
    (= candidate_pool) here so that the reranker has a wide enough pool to
    promote lower-ranked relevant documents.
    """
    sv = embed_sparse_query(query, model_name=bm25_model)
    return client.query_points(
        collection_name=coll_name,
        prefetch=[
            qmodels.Prefetch(query=dense_vec, using=DENSE_VECTOR_NAME, limit=prefetch_limit),
            qmodels.Prefetch(
                query=qmodels.SparseVector(indices=sv.indices, values=sv.values),
                using=SPARSE_VECTOR_NAME, limit=prefetch_limit,
            ),
        ],
        query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
        limit=top_n,
        with_payload=True,
    )


def _visual_collection_ready(client, coll_name: str) -> bool:
    """True when the visual collection exists and holds at least one point.

    Used to auto-gate visual search: in the default (auto) mode we only pay the
    ColPali model load + query embed when there's actually something to search.
    Any error (missing collection, transport failure) is treated as not-ready.
    """
    try:
        info = client.get_collection(coll_name)
    except Exception:
        return False
    count = getattr(info, "points_count", None)
    return bool(count and count > 0)


def _rrf_merge_collections(per_collection: list[list[dict]], top_n: int, k: int = 60) -> list[dict]:
    """Fuse ranked hit lists from multiple collections with Reciprocal Rank Fusion.

    Each collection's native scores live on incomparable scales — text uses cosine
    or RRF (~0-1) while the visual collection uses ColPali MaxSim (a sum over query
    tokens, ~10-40).  Merging by raw score lets visual hits crowd out every text
    hit.  RRF discards score magnitude and fuses by rank instead, so a rank-0 text
    hit and a rank-0 visual hit compete fairly regardless of scale.

    Args:
        per_collection: one list per collection, each already ordered best-first.
        top_n: number of fused results to return.
        k: RRF damping constant (Qdrant's fusion default is 60).

    Returns:
        Flat list of the original hit dicts, best-first by RRF, length <= top_n.
        Ties (same rank across collections) break toward earlier collections, so
        callers should pass the text collection before the visual one.
    """
    scored = []
    for coll_index, hits in enumerate(per_collection):
        for rank, hit in enumerate(hits):
            rrf = 1.0 / (k + rank + 1)
            scored.append((rrf, coll_index, rank, hit))
    # -rrf: higher fused score first. coll_index/rank: deterministic, text-first ties.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [hit for _, _, _, hit in scored[:top_n]]


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
    from pathlib import Path

    top_n = cfg.get("search", {}).get("top_n", 5)
    repo_root = Path(find_config()).parent

    # Compute effective retrieval depth.
    # When reranking is enabled, fetch candidate_pool docs per collection so
    # the cross-encoder has a wide enough pool to promote lower-ranked relevant
    # documents.  When reranking is off, fetch exactly top_n (unchanged).
    rr_cfg = cfg.get("search", {}).get("rerank", {})
    rerank_enabled = rr_cfg.get("enabled", False)
    candidate_pool = rr_cfg.get("candidate_pool", 30)
    fetch_limit = max(candidate_pool, top_n) if rerank_enabled else top_n

    # Get all collections to search
    try:
        collections = get_search_collections(cfg, "repo")
    except ValueError:
        # Fall back to default collections
        collections = [collection_name(cfg, "doc")]
        # List visual unless explicitly opted out; run_search gates the actual query
        # on collection readiness, so an absent/empty collection costs nothing.
        if cfg.get("embed", {}).get("colpali_enabled", None) is not False:
            collections.append(f"{cfg['project_name']}_visual")
    
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e
    
    # Search each collection independently, then fuse across them by rank (RRF)
    # so incomparable score scales (text cosine/RRF vs visual ColPali MaxSim)
    # can't crowd each other out.
    per_collection: list[list[dict]] = []

    for coll_name in collections:
        coll_results: list[dict] = []
        try:
            if coll_name.endswith("_visual"):
                # Visual collection search using ColPali
                from carta.embed.colpali import is_colpali_available, ColPaliEmbedder, ColPaliError

                embed_cfg = cfg.get("embed", {})
                # Tri-state colpali_enabled: False = hard opt-out; True/None(auto) = on,
                # but only when ColPali is importable AND the collection has content.
                # The readiness check runs before the (expensive) model load so projects
                # with no visual content pay nothing.
                if embed_cfg.get("colpali_enabled", None) is False:
                    continue
                if not is_colpali_available():
                    continue
                if not _visual_collection_ready(client, coll_name):
                    continue

                model_name = embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0-hf")
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
                        limit=fetch_limit,
                        with_payload=True,
                    )
                    
                    for r in response.points:
                        payload = r.payload or {}
                        coll_results.append({
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

                hybrid_cfg = cfg.get("search", {}).get("hybrid", {})
                is_hybrid = collection_is_hybrid(client, coll_name)

                if hybrid_cfg.get("enabled", False) and is_hybrid:
                    # Hybrid BM25+dense with RRF fusion.
                    # Pass fetch_limit as the fusion limit so the reranker
                    # receives a full candidate_pool rather than just top_n.
                    response = _hybrid_query_collection(
                        client, coll_name, query, query_vec, fetch_limit,
                        prefetch_limit=hybrid_cfg.get("prefetch_limit", 40),
                        bm25_model=hybrid_cfg.get("bm25_model", "Qdrant/bm25"),
                    )
                elif is_hybrid:
                    # Hybrid collection schema but hybrid search disabled — use named dense vector
                    response = client.query_points(
                        collection_name=coll_name,
                        query=query_vec,
                        using=DENSE_VECTOR_NAME,
                        limit=fetch_limit,
                        with_payload=True,
                    )
                else:
                    # Legacy unnamed-dense collection
                    response = client.query_points(
                        collection_name=coll_name,
                        query=query_vec,
                        limit=fetch_limit,
                        with_payload=True,
                    )
                
                for r in response.points:
                    payload = r.payload or {}
                    coll_results.append({
                        "score": r.score,
                        "source": payload.get("file_path", payload.get("slug", "")),
                        "excerpt": payload.get("text", ""),
                        "type": "text",
                    })

            per_collection.append(coll_results)
        except Exception as e:
            err_str = str(e).lower()
            # 404 / collection not found — skip silently (collection may not exist yet)
            if "404" in err_str or "not found" in err_str or "doesn't exist" in err_str:
                continue
            # Connection/transport errors — surface as actionable error
            if any(kw in err_str for kw in ("connection refused", "connection error", "network", "timeout", "unreachable")):
                raise RuntimeError(
                    f"Cannot reach Qdrant — is it running? "
                    f"Start it with: carta doctor --fix\n(Detail: {e})"
                ) from e
            # Other unexpected errors — skip collection, don't break entire search
            continue
    
    # Fuse across collections by rank (RRF) — scale-free, so visual MaxSim scores
    # can't swamp text cosine/RRF scores. fetch_limit keeps the rerank pool wide.
    all_results = _rrf_merge_collections(per_collection, fetch_limit)

    # Optional second-stage cross-encoder reranking (opt-in via search.rerank.enabled)
    if rerank_enabled and all_results:
        from carta.search.rerank import rerank_hits
        pool = all_results[:candidate_pool]
        # rerank_hits reads chunk text from key "text"; run_search stores it as "excerpt"
        for h in pool:
            h["text"] = h.get("excerpt", "")
        all_results = rerank_hits(
            query,
            pool,
            model_name=rr_cfg.get("model", "BAAI/bge-reranker-base"),
            top_n=top_n,
        )
        # Strip transient keys so returned dicts have a stable shape
        # regardless of whether reranking ran.
        for _h in all_results:
            _h.pop("text", None)
            _h.pop("rerank_score", None)

    return all_results[:top_n]
