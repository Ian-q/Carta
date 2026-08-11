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
from typing import Iterator, Optional

import yaml
from qdrant_client import QdrantClient
from qdrant_client import models as qmodels
from qdrant_client.models import Filter

from carta import __version__ as _CARTA_VERSION
from carta.config import collection_name, collection_for_doc_type, find_config
from carta.embed.parse import extract_pdf_text, extract_pdf_text_and_classify, extract_markdown_text, chunk_text, _estimate_tokens, resolve_doc_title, apply_contextual_headers
from carta.embed.embed import (
    ensure_collection,
    upsert_chunks,
    get_embedding,
    upsert_visual_pages,
    collection_is_hybrid,
    _point_id_versioned,
    _visual_point_id,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    UPSERT_CLIENT_TIMEOUT_S,
)
from carta.embed.sparse import embed_sparse_query
from carta.embed.induct import generate_sidecar_stub, read_sidecar, write_sidecar, sidecar_path, iter_canonical_sidecars, SPREADSHEET_SUFFIXES
from carta.embed.tabular import (
    extract_spreadsheet_text, write_companion, companion_rel_path, OpenpyxlMissing,
)
from carta.embed.lifecycle import needs_rehash, compute_file_hash, mark_sidecar_stale, check_stale_alert, delete_other_points
from carta.embed.visual_queue import add_pending_pages, move_to_done, VISUAL_PENDING_KEY, VISUAL_DONE_KEY, queue_summary, format_summary_line
# NOTE: carta.embed.colpali is imported LAZILY, inside the functions that need it.
# It pulls in torch + transformers (~2.4s), and carta-hook imports run_search from
# this module on EVERY prompt — so a module-level import here costs that on every
# prompt in every project, just to read a boolean. Guarded by
# carta/embed/tests/test_import_cost.py.
from carta.embed.status import StatusWriter
from carta.vision.classifier import PageClass, PageAnalyzer

_IMAGE_HEAVY = {PageClass.TEXT_WITH_IMAGES, PageClass.FLATTENED, PageClass.VECTOR_DRAWING}


def _mark_or_collect_visual_pages(page_classes: list, cfg: dict, rel_path: str = "") -> dict:
    """Return sidecar updates queuing 1-indexed image-heavy pages when two_pass_visual is on.

    Deliberately independent of colpali_scoped_paths: ColPali scoping gates the
    ColPali embedder only (see _visual_embed_one_page) — the OCR/vision drain
    covers every file. The old scope gate here silently left out-of-scope PDFs
    with zero visual coverage (2026-07 dark-corpus incident).

    Args:
        page_classes: List of PageClass values, one per page (0-indexed position = page-1).
        cfg: Carta config dict. Reads embed.two_pass_visual.
        rel_path: Repo-relative path of the source file. Accepted for call-site
            compatibility but not used to gate queueing (see above).

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


_SUPPORTED_EXTENSIONS = [".pdf", ".md", ".csv", ".xlsx"]
_SUPPORTED_EXTENSIONS_SET = frozenset(_SUPPORTED_EXTENSIONS)


def _iter_inductable_files(docs_root: Path) -> Iterator[Path]:
    """Yield supported source files under docs_root, matching extensions
    case-insensitively so uppercase ``.PDF`` (and ``.MD``) are found like ``.pdf``.

    The scanner's induction check already lowercases suffixes
    (``check_embed_induction_needed``), so a case-sensitive ``rglob("*.pdf")`` here
    left uppercase-extension files perpetually flagged "needs induction" yet never
    auto-inducted — embeddable only via an explicit ``carta embed <file>``.
    """
    for p in docs_root.rglob("*"):
        if ".carta" in p.parts:
            continue  # derived artifacts (companions, sidecars) are never sources
        if p.is_file() and p.suffix.lower() in _SUPPORTED_EXTENSIONS_SET:
            yield p

# Maximum seconds to allow a single file's embed processing to run
FILE_TIMEOUT_S = 300

# Minimum candidate pool fetched before de-duplication, so the top_n SHOWN results
# can be top_n DISTINCT docs even when several high-ranked chunks share a doc.
_RESULT_POOL_FLOOR = 30

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
    """Find all sidecars under .carta/sidecars/ awaiting (re-)embedding.

    Picks the re-pickable statuses: ``pending`` (never embedded) plus
    ``embed_failed`` and ``partial`` — a transient embed failure or a partial
    upsert must be retried on the next run, not silently stranded as if done.

    Returns list of dicts: sidecar data + 'sidecar_path' + 'file_path'.
    """
    results = []
    sidecars_root = repo_root / ".carta" / "sidecars"
    if not sidecars_root.exists():
        return results
    for sc_path in sidecars_root.rglob("*.embed-meta.yaml"):
        data = read_sidecar(sc_path)
        if data is None or data.get("status") not in ("pending", "embed_failed", "partial"):
            continue
        current_path = data.get("current_path")
        if not current_path:
            continue
        source_file = repo_root / current_path
        if source_file.exists():
            results.append({**data, "sidecar_path": sc_path, "file_path": source_file})
    return results


def discover_stale_files(repo_root: Path) -> list[Path]:
    """Find source files whose content has changed since they were last embedded.

    A file is stale when it still exists, its sidecar recorded a ``file_hash``,
    and the file's current content hash differs from that recorded hash. (The
    old sidecar ``status: stale`` flag is no longer written — staleness now lives
    in the Qdrant payload — so staleness is recomputed on demand here. See #39.)

    Returns list of source file paths.
    """
    results = []
    for sc_path, data in iter_canonical_sidecars(repo_root):
        recorded_hash = data.get("file_hash")
        if not recorded_hash:
            continue
        source_file = repo_root / data["current_path"]
        if not source_file.exists():
            continue
        try:
            if compute_file_hash(source_file) != recorded_hash:
                results.append(source_file)
        except OSError:
            continue
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
    suffix = file_path.suffix.lower()
    tabular_meta: dict = {}
    if suffix == ".md":
        pages, frontmatter_meta = extract_markdown_text(file_path)
    elif suffix in SPREADSHEET_SUFFIXES:
        try:
            pages, tabular_meta = extract_spreadsheet_text(file_path)
        except OpenpyxlMissing as e:
            print(f"Warning: {e}", file=sys.stderr, flush=True)
            # Leave the sidecar re-pickable: the file is retried once openpyxl
            # is installed. No mtime/hash fields are stamped.
            return 0, {"status": "pending"}
        frontmatter_meta = {}
        # Companion note: transparency artifact, fail-open (not load-bearing).
        write_companion(
            repo_root, file_path.relative_to(repo_root),
            tabular_meta.get("companion_markdown", ""),
        )
    elif suffix == ".pdf" and cfg.get("embed", {}).get("two_pass_visual", True):
        try:
            analyzer = PageAnalyzer(cfg)
            pages, _page_classes_from_extraction = extract_pdf_text_and_classify(file_path, analyzer)
        except Exception as _cls_exc:
            # Classification failed: fall back to text-only extraction and skip inline vision
            # (fail closed — do not escalate to the heavy VLM path).
            print(
                f"Warning: two_pass_visual page classification failed for {file_path}: {_cls_exc}; "
                f"pages left unclassified — skipping inline vision for this file; two-pass visual "
                f"queueing also skipped — file has no visual coverage until flagged (carta flag) "
                f"or re-embedded",
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
    chunking_cfg = cfg.get("embed", {}).get("chunking", {})
    if chunking_cfg.get("contextual_header", True):
        doc_title = resolve_doc_title(frontmatter_meta, pages, file_path)
        apply_contextual_headers(
            raw_chunks, doc_title,
            include_section=chunking_cfg.get("contextual_header_section", True),
        )
    if progress:
        progress.step(f"embedding {len(raw_chunks)} chunks → Qdrant")
    elif verbose:
        print(f"    built {len(raw_chunks)} chunk(s); embedding + upserting...", flush=True)

    slug = file_info.get("slug", file_path.stem)
    generation = int(file_info.get("generation") or 1)
    metadata = {
        "slug": slug,
        "file_path": str(file_path.relative_to(repo_root)),
        "doc_type": file_info.get("doc_type", "unknown"),
        "doc_generation": generation,
    }
    if frontmatter_meta:
        metadata["frontmatter"] = frontmatter_meta
    if suffix in SPREADSHEET_SUFFIXES:
        metadata["derived"] = "spreadsheet"
        metadata["companion_path"] = str(
            companion_rel_path(file_path.relative_to(repo_root)))

    from carta.embed.enrichment import enrichment_suffix, source_rel_for_enrichment

    rel_of_file = file_path.relative_to(repo_root)
    if rel_of_file.name.endswith(enrichment_suffix(cfg)):
        src_rel = source_rel_for_enrichment(rel_of_file, cfg)
        if src_rel and (repo_root / src_rel).is_file():
            metadata["enriches"] = str(src_rel)

    enriched = [{**metadata, **chunk} for chunk in raw_chunks]
    # expected_text counts only non-empty chunks — upsert_chunks drops empty ones
    # before embedding, so the cleanup gate must compare against the same set.
    # kept_text_chunks mirrors the same filter so we can derive their point IDs.
    kept_text_chunks = [c for c in enriched if (c.get("text") or "").strip()]
    expected_text = len(kept_text_chunks)

    # Zero usable text: for non-PDF files there's no image-chunk rescue path, so
    # flag immediately. PDFs may still produce content via the vision path below.
    if expected_text == 0 and suffix != ".pdf":
        if suffix in SPREADSHEET_SUFFIXES:
            zero_status = "no_text_content"
            print(
                f"Note: {file_path.name}: no text-bearing cells — nothing embedded "
                f"(numeric-only data is deliberately not indexed)",
                flush=True,
            )
        else:
            zero_status = "extraction_failed"
            print(
                f"Warning: {file_path.name}: 0 extractable characters — "
                f"skipped (empty or unreadable file)",
                flush=True,
            )
        return 0, {
            "status": zero_status,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": 0,
            "image_count": 0,
            "image_chunks": 0,
            "file_mtime": os.path.getmtime(str(file_path)),
            "visual_pages": 0,
            "_vision_events": [],
        }

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
    expected_images = 0  # tracks chunks attempted; must equal image_chunk_count for cleanup gate
    kept_image_chunks: list[dict] = []  # non-empty image chunks actually upserted (for ID derivation)
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
            # Pass the repo-relative path for call-site compatibility (the sidecar's
            # current_path); _mark_or_collect_visual_pages no longer uses it to gate
            # queueing — colpali_scoped_paths is scope-independent for queueing/OCR,
            # it only gates the ColPali embed step later in the pass-2 drain
            # (_visual_embed_one_page). Every image-heavy page gets queued here
            # regardless of scope. _skip_inline_vision stays True regardless, so the
            # heavy inline VLM/ColPali path is never run for a two-pass file.
            _rel_path = (
                str(file_path.relative_to(repo_root))
                if file_path.is_relative_to(repo_root) else str(file_path)
            )
            _visual_queue_updates = _mark_or_collect_visual_pages(
                _page_classes_from_extraction, cfg, _rel_path
            )
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

        # A `carta flag` force-queue (deep_scan: requested) must survive a text
        # re-embed / repair that happens in between the flag and the visual
        # drain. _mark_or_collect_visual_pages only returns THIS pass's
        # freshly-classified image-heavy pages — merging that in via a plain
        # dict-key overwrite (below, at the sidecar_updates.update() merge)
        # would silently shrink visual_pending to whatever this pass's
        # classifier calls image-heavy, dropping pages the flag force-queued
        # that now look ordinary. Union with whatever was already pending
        # while the flag is still live.
        if file_path.is_relative_to(repo_root):
            _existing_sc = read_sidecar(sidecar_path(file_path, repo_root)) or {}
            if _existing_sc.get("deep_scan") == "requested":
                _prior_pending = _existing_sc.get(VISUAL_PENDING_KEY) or []
                _fresh_pending = _visual_queue_updates.get(VISUAL_PENDING_KEY) or []
                _unioned_pending = sorted(set(_prior_pending) | set(_fresh_pending))
                if _unioned_pending:
                    _visual_queue_updates[VISUAL_PENDING_KEY] = _unioned_pending

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
                                "doc_generation": generation,
                                "page_num": desc["page_num"],
                                "image_index": desc["image_index"],
                                "chunk_index": len(raw_chunks) + len(image_chunks),
                                "text": part_text,
                                # Phase 999.4: extraction provenance
                                "model_used": desc.get("model_used", "llava"),
                                "content_type": desc.get("content_type", "visual"),
                            })
                    # Count only non-empty chunks — upsert_chunks drops empty ones,
                    # and a clean drop must not read as a partial upsert to the gate.
                    kept_image_chunks = [c for c in image_chunks if (c.get("text") or "").strip()]
                    expected_images = len(kept_image_chunks)
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
                                "doc_generation": generation,
                                "page_num": desc["page_num"],
                                "image_index": desc["image_index"],
                                "chunk_index": len(raw_chunks) + len(image_chunks),
                                "text": desc["text"],
                            })
                        kept_image_chunks = [c for c in image_chunks if (c.get("text") or "").strip()]
                        expected_images = len(kept_image_chunks)
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
        "generation": generation,  # persist generation so bulk sidecars don't stay at 0
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

    # Nothing extractable anywhere: no text was attempted, no image chunks were
    # attempted, and no pages are queued for pass-2 visual embedding.
    if (expected_text == 0 and expected_images == 0
            and not (_visual_queue_updates.get(VISUAL_PENDING_KEY) or [])):
        # Give OCR a chance before declaring failure (#38 part 1): a PDF with no
        # extractable text is almost certainly scanned/flattened, so queue every
        # page for the `--visual` drain (glm-ocr text + ColPali) rather than
        # dead-ending. extraction_failed is reserved for inputs OCR can't help:
        # non-PDFs, zero-page PDFs, or runs with visual/OCR disabled.
        embed_cfg = cfg.get("embed", {})
        ocr_recoverable = (
            file_path.suffix == ".pdf"
            and len(pages) > 0
            and embed_cfg.get("two_pass_visual", True)
            and embed_cfg.get("vision_routing", "auto") != "off"
        )
        if ocr_recoverable:
            add_pending_pages(sidecar_updates, list(range(1, len(pages) + 1)))
            if verbose:
                print(
                    f"    {file_path.name}: 0 extractable text — queued "
                    f"{len(pages)} page(s) for OCR (run `carta embed --visual`)",
                    flush=True,
                )
        else:
            sidecar_updates["status"] = "extraction_failed"
            print(
                f"Warning: {file_path.name}: 0 extractable characters — skipped "
                f"(scanned PDF? OCR may be required)",
                flush=True,
            )

    # File fully embedded — clear any per-page resume checkpoint.
    if file_path.suffix == ".pdf":
        cp = _vision_checkpoint_path(repo_root, slug)
        try:
            cp.unlink(missing_ok=True)
        except OSError:
            pass

    # Delete every point for this file except the ones just written, but only when the
    # upsert was complete.  A partial upsert leaves the new chunks incomplete; removing
    # existing points would then lose data from BOTH old and new.  Require exact counts.
    # ID-set-based cleanup (HasIdCondition) subsumes generation arithmetic and additionally
    # removes: legacy slug-keyed duplicates, tail chunks of shrunken files, and any points
    # from a same-generation re-embed that the old doc_generation!=g filter would have spared.
    rel_path = str(file_path.relative_to(repo_root))
    if count + image_chunk_count > 0 and count == expected_text and image_chunk_count == expected_images:
        coll = collection_for_doc_type(cfg, file_info.get("doc_type", "unknown"))
        keep_ids = [
            _point_id_versioned(
                c.get("file_path") or c["slug"], c["chunk_index"], c.get("doc_generation", 1)
            )
            for c in kept_text_chunks + kept_image_chunks
        ]
        delete_other_points(client, coll, rel_path=rel_path, keep_ids=keep_ids)
    elif count + image_chunk_count > 0:
        print(
            f"Warning: partial upsert for {rel_path} ({count}/{expected_text} text, "
            f"{image_chunk_count}/{expected_images} image) — keeping previous generation's points",
            flush=True,
        )

    # Honest success accounting: if text/image chunks were attempted but not all
    # persisted, do NOT report a clean "embedded". Downgrade to a re-pickable
    # status so discovery retries the file and the summary/operator see the gap.
    # Files whose pages are queued for the visual/OCR drain are not failures —
    # their text legitimately lands in pass 2 — so they are excluded here.
    visual_pending = bool(_visual_queue_updates.get(VISUAL_PENDING_KEY))
    attempted = expected_text + expected_images
    persisted = count + image_chunk_count
    if sidecar_updates.get("status") == "embedded" and attempted > 0 and not visual_pending:
        if persisted == 0:
            sidecar_updates["status"] = "embed_failed"
        elif persisted < attempted:
            sidecar_updates["status"] = "partial"

    # This file IS an enrichment doc (metadata["enriches"] was set above): stamp
    # the SOURCE's sidecar (enrichment_path, enrichment_source_hash, deep_scan
    # requested->done) — but only once the embed's FINAL status (after the
    # honest-success-accounting downgrades above) is genuinely "embedded". A
    # total upsert failure (embed_failed) or a partial upsert must NOT stamp —
    # otherwise the source records "enrichment ingested" against its own
    # CURRENT file_hash, which enrichment_is_stale can then never flag as
    # stale, permanently hiding the failure.
    if metadata.get("enriches") and sidecar_updates.get("status") == "embedded":
        from carta.embed.enrichment import record_enrichment
        record_enrichment(repo_root, Path(metadata["enriches"]), rel_of_file)

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


def _delete_visual_orphans(client, cfg: dict, rel_path: str, keep_page_nums: list[int]) -> None:
    """Sweep stale visual points for one file.

    Deletes every ``{project}_visual`` point for ``rel_path`` except the stable
    IDs of ``keep_page_nums``. Mirrors the text lane's post-upsert
    ``delete_other_points`` call: id-set-based, so it removes legacy
    slug-keyed points, pre-fix generation-less points, and pages the document no
    longer has — regardless of doc_generation. Best-effort (delete_other_points
    retries and never raises).
    """
    coll = f"{cfg['project_name']}_visual"
    keep_ids = [_visual_point_id(rel_path, p) for p in keep_page_nums]
    delete_other_points(client, coll, rel_path=rel_path, keep_ids=keep_ids)


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
            rel_path = str(file_path.relative_to(repo_root))
            _delete_visual_orphans(client, cfg, rel_path, [p["page_num"] for p in visual_pages])
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


def _drain_sort_key(item: tuple, triage_paths: list[str]) -> tuple:
    """Drain order: flagged (oldest request first) -> triage-path prefixes -> FIFO.

    Ties are preserved via stable sort: files within the same tier remain in
    discovery order (no path-based alphabetization).
    """
    _sc_path, sc = item
    rel = str(sc.get("current_path") or "")
    if sc.get("priority") == "high":
        return (0, str(sc.get("deep_scan_requested_at") or ""))
    if any(rel.startswith(p) for p in triage_paths):
        return (1, "")
    return (2, "")


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


def _filter_visual_pending_in_scope(queued: list[tuple], scopes: list) -> list[tuple]:
    """Drop (sidecar_path, sidecar) pairs whose source (``current_path``) is out of
    ``colpali_scoped_paths``.

    NOT called from ``run_visual_embed`` anymore (spec Component 1): the OCR/vision
    drain covers every queued file regardless of scope, and only the ColPali step
    inside ``_visual_embed_one_page`` honors ``colpali_scoped_paths``. Kept as a
    standalone helper (unit-tested) in case a future caller needs scope-filtered
    queue slicing without pulling in the drain's OCR/ColPali side effects.

    Empty ``scopes`` means no restriction (backward compatible).
    """
    if not scopes:
        return queued
    return [
        (sc_path, sc)
        for (sc_path, sc) in queued
        if _colpali_path_in_scope(sc.get("current_path") or "", scopes)
    ]


def _visual_chunk_index_pass2(page: int, i: int) -> str:
    """Return the chunk_index token used for pass-2 visual/OCR text chunks.

    Pass-2 chunks set ``chunk_index`` to this string (e.g. ``"visual:1:0"``).
    ``upsert_chunks`` then derives the Qdrant point ID via
    ``_point_id_versioned(file_path, chunk_index, generation)``
    → ``md5("{file_path}:visual:{page}:{i}:g{gen}")``.

    This namespace is structurally disjoint from pass-1 text chunks, which
    always use integer chunk_index values (e.g. ``md5("{file_path}:0:g1")``).
    An integer can never equal the string ``"visual:{page}:{i}"``, so collision
    between pass-1 and pass-2 chunks for the same file is impossible by
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
    deep: bool = False,
) -> bool:
    """OCR text + ColPali for a single 1-indexed page. Raise on failure.

    (a) glm-ocr text for the page via SmartRouter → upsert_chunks (hybrid text index).
        Pass-2 chunks receive a pass-2-specific chunk_index token so their point
        IDs are disjoint from pass-1 text chunks for the same file. Runs
        unconditionally — the OCR/vision drain is not scoped by colpali_scoped_paths.
        A file flagged for deep scan (``deep=True``) or a page that classifies
        VECTOR_DRAWING routes through ``router.extract_page_deep`` (high-DPI
        tiled two-prompt extraction) instead of the normal ``router._route``.
    (b) ColPali for the page via ColPaliEmbedder.embed_pdf_pages(page_nums=[page])
        → upsert_visual_pages (_visual collection). Gated by colpali_scoped_paths
        (_colpali_path_in_scope) — the only step in this function that scope gates.

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

            from carta.vision.classifier import PageClass

            profile = router.analyzer.analyze(fitz_page)
            if deep or profile.page_class is PageClass.VECTOR_DRAWING:
                chunks = router.extract_page_deep(fitz_page, page)
            else:
                chunks = router._route(fitz_page, page, profile, doc)
        finally:
            doc.close()

    if chunks:
        image_chunks = []
        # Stamp the sidecar's current generation onto every pass-2 chunk
        # (observability metadata). Cleanup is ID-set-based: a text re-embed
        # deletes these points and re-queues the pages for re-drain.
        doc_generation = int(sidecar.get("generation") or 1)
        for chunk in chunks:
            for part_text in _split_vision_text(chunk.get("text", ""), max_tokens):
                i = len(image_chunks)
                entry = {
                    "slug": slug,
                    "file_path": current_path,
                    "doc_type": "image_description",
                    "doc_generation": doc_generation,
                    "page_num": page,
                    "image_index": chunk.get("image_index", 0),
                    # Use a pass-2-specific chunk_index token so the Qdrant point
                    # ID (derived by upsert_chunks as md5("{file_path}:{chunk_index}:g{gen}"))
                    # is disjoint from pass-1 text chunks (which use integer indices).
                    "chunk_index": _visual_chunk_index_pass2(page, i),
                    "text": part_text,
                    "model_used": chunk.get("model_used", "glm-ocr"),
                    "content_type": chunk.get("content_type", "visual"),
                    "source": "visual_drainer",
                }
                # Deep-tier chunks (router.extract_page_deep) carry tile/extraction —
                # pass them through so they land in the Qdrant payload for free
                # (build_point copies every non-text chunk key).
                if "tile" in chunk:
                    entry["tile"] = chunk["tile"]
                if "extraction" in chunk:
                    entry["extraction"] = chunk["extraction"]
                image_chunks.append(entry)
        # Honour the docstring contract: a short upsert (Qdrant/Ollama hiccup)
        # must raise so the page stays in visual_pending, never be silently
        # marked done with its OCR text lost. upsert_chunks legitimately drops
        # empty-text chunks, so compare against the non-empty expected count.
        expected_text = sum(1 for c in image_chunks if (c.get("text") or "").strip())
        stored_text = upsert_chunks(image_chunks, cfg, client=client)
        if stored_text < expected_text:
            raise RuntimeError(
                f"OCR upsert incomplete for page {page} of {current_path}: "
                f"stored {stored_text}/{expected_text} chunk(s) — leaving page pending"
            )
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

    scopes = (cfg.get("embed", {}) or {}).get("colpali_scoped_paths", []) or []
    if scopes and not _colpali_path_in_scope(current_path, scopes):
        pass  # out of ColPali scope: OCR chunks above still upserted
    else:
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
            # ColPali vectors are the expensive, irreplaceable artifact of this pass;
            # a short upsert must raise so the page is retried, not marked done empty.
            stored_visual = upsert_visual_pages(visual_pages, cfg, client=client)
            if stored_visual < len(visual_pages):
                raise RuntimeError(
                    f"ColPali upsert incomplete for page {page} of {current_path}: "
                    f"stored {stored_visual}/{len(visual_pages)} page(s) — leaving page pending"
                )
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

    from carta.embed.colpali import is_colpali_available

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

    client = QdrantClient(url=cfg["qdrant_url"], timeout=UPSERT_CLIENT_TIMEOUT_S)
    queued = _discover_visual_pending(repo_root)
    # colpali_scoped_paths is NOT applied here: the drain OCRs every queued file
    # regardless of scope — only the ColPali embed step inside
    # _visual_embed_one_page honors colpali_scoped_paths (via _colpali_path_in_scope).

    # Sort: flagged files (high priority, oldest first), then triage paths, then FIFO
    triage_paths = (cfg.get("embed", {}) or {}).get("visual_triage_paths", []) or []
    queued.sort(key=lambda it: _drain_sort_key(it, triage_paths))

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
            rel_path = sc.get("current_path") or ""
            file_failed = False
            deep = sc.get("deep_scan") == "requested"
            for page in list(sc.get(VISUAL_PENDING_KEY, []) or []):
                idx += 1
                status.file_start(idx, f"page {page} of {rel_path}")
                try:
                    _visual_embed_one_page(sc, page, cfg, client, repo_root, router, embedder, verbose, deep=deep)
                    move_to_done(sc, page)
                    _update_sidecar(sc_path, {
                        VISUAL_PENDING_KEY: sc[VISUAL_PENDING_KEY],
                        VISUAL_DONE_KEY: sc[VISUAL_DONE_KEY],
                    })
                    summary["pages_embedded"] += 1
                    status.file_done(embedded=1)
                except Exception as e:
                    file_failed = True
                    summary["pages_failed"] += 1
                    status.file_done(errors=1)
                    print(
                        f"  visual: page {page} of {sc.get('current_path')} failed: {e} "
                        f"(left pending)",
                        flush=True,
                    )
            # Sweep the file's stale visual points only after a clean drain — never
            # delete a page that's going to be retried (mirrors the text lane's
            # "clean up only after complete success" guard after upsert_chunks).
            if rel_path and not file_failed and sc.get(VISUAL_DONE_KEY):
                _delete_visual_orphans(client, cfg, rel_path, list(sc[VISUAL_DONE_KEY]))
                if sc.get("deep_scan") == "requested":
                    _update_sidecar(sc_path, {"deep_scan": "done"})
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
        # Extension-preserving sidecars (data.csv.embed-meta.yaml) already carry
        # the full source filename in the stem — resolve directly.
        if Path(stem).suffix.lower() in SPREADSHEET_SUFFIXES:
            candidate = repo_root / parent_dirs / stem
            if candidate.exists():
                data["current_path"] = str(parent_dirs / stem)
                _update_sidecar(sc_path, data)
                healed += 1
            continue
        for ext in _SUPPORTED_EXTENSIONS:
            # Extension-preserving types are handled above; adopting one here
            # would mint a non-canonical (extension-stripped) sidecar that
            # discover_pending_files would still embed alongside the canonical
            # stub auto-induction creates — double-indexing one source.
            if ext in SPREADSHEET_SUFFIXES:
                continue
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
    """Return sidecar paths under .carta/sidecars/ whose current_path source no longer exists.

    Uses iter_canonical_sidecars, so corrupt sidecars, those without a
    current_path, and misplaced/nested junk copies are skipped (a junk copy is
    not a real sidecar of this repo even if its current_path source is missing).
    """
    orphans = []
    for sc_path, data in iter_canonical_sidecars(repo_root):
        if not (repo_root / data["current_path"]).exists():
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

    if not force and current_hash == old_hash and old_hash is not None:
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

    # Prepare lifecycle updates.
    # VISUAL_DONE_KEY is reset to [] so add_pending_pages no longer excludes these pages;
    # pass-1 re-queues image-heavy pages and the visual drainer re-processes them.
    # VISUAL_PENDING_KEY is intentionally omitted — pass-1's fresh queuing merges into it
    # and any stale pending pages re-drain idempotently (point IDs overwrite in place).
    # NOTE: "status" and "stale_as_of" are intentionally excluded here — they are set
    # after the embed completes based on what _embed_one_file returns (Bug A fix).
    lifecycle_updates = {
        "generation": new_generation,
        "file_hash": current_hash,
        "file_mtime": current_mtime,
        "last_hash_check_at": now.isoformat(),
        "version_history": version_history,
        VISUAL_DONE_KEY: [],
    }

    # Mark chunks as stale in Qdrant (with migration boundary guard)
    if sidecar_data.get("sidecar_id"):
        client = QdrantClient(url=cfg["qdrant_url"], timeout=UPSERT_CLIENT_TIMEOUT_S)
        mark_sidecar_stale(client, collection_name(cfg, "doc"), sidecar_data.get("sidecar_id"), now)

    # Proceed with re-embedding
    file_info = {
        "slug": sidecar_data.get("slug", file_path.stem),
        "doc_type": sidecar_data.get("doc_type", "unknown"),
        "sidecar_path": sc_path,
        "file_path": file_path,
        "generation": new_generation,
    }

    client = QdrantClient(url=cfg["qdrant_url"], timeout=UPSERT_CLIENT_TIMEOUT_S)
    ensure_collection(client, collection_name(cfg, "doc"))

    chunking = cfg.get("embed", {}).get("chunking", {})
    max_tokens = chunking.get("max_tokens", 400)
    overlap_fraction = chunking.get("overlap_fraction", 0.15)

    count, sidecar_updates = _embed_one_file(
        file_path, file_info, cfg, client, repo_root, max_tokens, overlap_fraction, verbose, progress
    )
    # A "pending" return means extraction was skipped (missing optional
    # dependency): stamp NO lifecycle fields — a hash/mtime stamp here would
    # let the mtime fast-path treat the file as already embedded and never
    # re-pick it on this surface once the dependency is installed.
    if sidecar_updates.get("status") == "pending":
        _update_sidecar(sc_path, {"status": "pending"})
        return {"status": "skipped", "reason": "extraction skipped — optional dependency missing"}
    # Merge lifecycle updates with embedding updates.
    # lifecycle_updates must NOT clobber the status _embed_one_file chose ("embedded"
    # or "extraction_failed") — apply lifecycle fields first, then let embed results win.
    merged = {**lifecycle_updates, **sidecar_updates}
    merged.setdefault("status", "embedded")   # belt-and-braces: _embed_one_file always sets it
    merged["stale_as_of"] = None              # re-embed completed — no longer stale
    merged.pop("_vision_events", None)        # temp key — never written to sidecar
    _update_sidecar(sc_path, merged)
    return {"status": "ok", "chunks": count}


def run_embed(repo_root: Path, cfg: dict, verbose: bool = False, progress=None) -> dict:
    """Run the embed pipeline on all pending files under repo_root.

    Args:
        repo_root: root directory to scan for .embed-meta.yaml sidecars.
        cfg: carta config dict.
        verbose: if True, print progress to stdout. If False, stdout is silent.

    Returns:
        {"embedded": int, "skipped": int, "extraction_failed": int,
         "no_text_content": int,
         "failed": list[str], "partial": list[str],
         "errors": list[str], "timed_out": list[str]}

    "failed"/"partial" hold the names of files whose chunks did not fully persist
    (transient Ollama/Qdrant errors). Their sidecars keep a re-pickable status so
    discover_pending_files retries them on the next run — they are never counted
    as "embedded".
    """
    summary: dict = {"embedded": 0, "skipped": 0, "extraction_failed": 0,
                     "no_text_content": 0,
                     "failed": [], "partial": [], "errors": [], "timed_out": []}

    # Migrate any co-located sidecars from old format to .carta/sidecars/
    migrate_sidecars(repo_root, verbose=verbose)

    # Pre-flight: check Qdrant reachability with a short timeout
    if verbose:
        print("carta embed: checking Qdrant connectivity...", flush=True)
    try:
        QdrantClient(url=cfg["qdrant_url"], timeout=5).get_collections()
    except Exception as e:
        err = (
            f"carta embed: ERROR — Qdrant is not reachable at {cfg['qdrant_url']}.\n"
            f"  Is Docker running? Start it and try again.\n"
            f"  Detail: {e}"
        )
        print(err, file=sys.stderr, flush=True)
        summary["errors"].append(err)
        return summary

    # Working client for the embed loop: a longer timeout than the fail-fast
    # preflight above so bulk dense+sparse upserts don't spuriously time out
    # (and silently drop a batch) when Qdrant is briefly busy under a large re-embed.
    client = QdrantClient(url=cfg["qdrant_url"], timeout=UPSERT_CLIENT_TIMEOUT_S)

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

    # Auto-induct any supported files that lack a sidecar (e.g. after sidecar deletion).
    # Extension matching is case-insensitive so uppercase .PDF is inducted like .pdf.
    docs_root_path = repo_root / cfg.get("docs_root", "docs/")
    if docs_root_path.is_dir():
        for file_path in _iter_inductable_files(docs_root_path):
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
            # file_timeout_s <= 0 means UNBOUNDED (matches visual_timeout_s "0 =
            # unbounded"). A literal join(0) returns instantly and flags every file
            # as TIMEOUT — embedding nothing while still exiting 0 (audit CA-3).
            join_timeout = file_timeout_s if (file_timeout_s and file_timeout_s > 0) else None
            worker.join(timeout=join_timeout)
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
                st = sidecar_updates.get("status")
                if st == "embed_failed":
                    summary["failed"].append(file_path.name)
                    perf_status = "failed"
                    status.file_done(errors=1)
                elif st == "partial":
                    summary["partial"].append(file_path.name)
                    perf_status = "partial"
                    status.file_done(errors=1)
                elif st in ("extraction_failed", "no_text_content"):
                    summary[st] += 1
                    perf_status = "ok"
                    status.file_done(embedded=1, chunks=count)
                elif st == "pending":
                    # spreadsheet skipped for a missing optional dependency —
                    # stays re-pickable, count as skipped
                    summary["skipped"] += 1
                    perf_status = "skip"
                    status.file_done(skipped=1)
                else:
                    summary["embedded"] += 1
                    perf_status = "ok"
                    status.file_done(embedded=1, chunks=count)
                _write_perf_log_entry(perf_log_path, {
                    **perf_context, "file": rel_file, "status": perf_status,
                    "chunks": count, "elapsed_s": round(elapsed, 2),
                    "vision_strategies": _summarize_vision_strategies(vision_events),
                })
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


# Qdrant's server-side RRF uses k=2. Client-side fusion starts here to preserve
# ordering exactly; see docs/superpowers/specs/2026-08-09-retrieval-path-repair-
# and-tracing-design.md Component 2.
QDRANT_RRF_K = 2


def _lane_queries(client: QdrantClient, coll_name: str, query: str, dense_vec: list[float],
                  prefetch_limit: int, bm25_model: str,
                  query_filter: Filter | None = None) -> tuple[list, list]:
    """Query the dense and sparse lanes separately and return both point lists."""
    sv = embed_sparse_query(query, model_name=bm25_model)
    dense_resp = client.query_points(
        collection_name=coll_name, query=dense_vec, using=DENSE_VECTOR_NAME,
        limit=prefetch_limit, query_filter=query_filter, with_payload=True,
    )
    sparse_resp = client.query_points(
        collection_name=coll_name,
        query=qmodels.SparseVector(indices=sv.indices, values=sv.values),
        using=SPARSE_VECTOR_NAME, limit=prefetch_limit,
        query_filter=query_filter, with_payload=True,
    )
    return dense_resp.points, sparse_resp.points


def _fuse_lanes(dense_points, sparse_points, top_n: int,
                k: int = QDRANT_RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion over two lanes, returning ranks alongside scores.

    Mirrors Qdrant's server-side RRF at k=2. A point present in only one lane is
    admitted with the other lane recorded as None — dropping it would be a silent
    recall bug.
    """
    acc: dict = {}
    for lane, points in (("dense", dense_points), ("sparse", sparse_points)):
        for rank, p in enumerate(points):
            entry = acc.setdefault(
                p.id, {"point": p, "score": 0.0,
                       "ranks": {"dense": None, "sparse": None}})
            entry["score"] += 1.0 / (k + rank)
            entry["ranks"][lane] = rank
    fused = sorted(acc.values(), key=lambda e: (-e["score"], str(e["point"].id)))
    return fused[:top_n]


def _hybrid_query_collection(client, coll_name, query, dense_vec, top_n,
                              prefetch_limit, bm25_model, query_filter=None,
                              rrf_k: int = QDRANT_RRF_K):
    """Run a hybrid BM25+dense query, fused client-side so per-lane ranks are available.

    Fetches `prefetch_limit` candidates from each of the dense and sparse
    indexes, then fuses them with Reciprocal Rank Fusion (matching Qdrant's
    server-side RRF at k=2) and returns the top `top_n` results.

    `top_n` controls how many fused results are returned. When reranking is
    enabled, callers should pass `fetch_limit` (= candidate_pool) here so that
    the reranker has a wide enough pool to promote lower-ranked relevant
    documents.

    `query_filter` (optional) is applied to BOTH lanes so the filter takes
    effect before fusion (used by run_focus to scope to one file).

    Returns a list of dicts: `{"point": <qdrant point>, "score": float,
    "ranks": {"dense": int | None, "sparse": int | None}}`.
    """
    dense_points, sparse_points = _lane_queries(
        client, coll_name, query, dense_vec, prefetch_limit, bm25_model, query_filter,
    )
    return _fuse_lanes(dense_points, sparse_points, top_n, rrf_k)


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


def _apply_visual_cap(ordered: list[dict], limit: int, visual_max_ratio: float = 1.0) -> list[dict]:
    """Admit up to ``limit`` hits from an already-ordered list, capping the visual share.

    Caps ``type == "visual"`` hits at ``round(visual_max_ratio * limit)``; overflow
    visual is diverted and the freed slots are backfilled with deeper non-visual hits,
    input order preserved among everything admitted. ``visual_max_ratio >= 1.0`` disables
    the cap. Returns a list of length <= limit.
    """
    visual_cap = round(visual_max_ratio * limit)
    result: list[dict] = []
    overflow: list[dict] = []
    visual_admitted = 0
    for hit in ordered:
        if len(result) >= limit:
            break
        if hit.get("type") == "visual":
            if visual_admitted < visual_cap:
                result.append(hit)
                visual_admitted += 1
            else:
                overflow.append(hit)
        else:
            result.append(hit)
    # Text too shallow to fill the pool: restore diverted visual, still in order.
    if len(result) < limit and overflow:
        result.extend(overflow[: limit - len(result)])
    return result


_FOCUS_DEFAULT_LIMIT = 15  # passages returned by a deep focus query
_FOCUS_OUTLINE_SCROLL_LIMIT = 10_000  # raise the limit if any single file exceeds this chunk count


def _normalize_source(source: str) -> str:
    """Strip a trailing ' (page N)' suffix (the visual-hit source form) to the bare file_path."""
    return re.sub(r"\s*\(page\s+\S+\)\s*$", "", source).strip()


def _file_filter(source: str) -> Filter:
    """Qdrant filter matching points whose file_path payload equals `source`."""
    return Filter(must=[qmodels.FieldCondition(
        key="file_path", match=qmodels.MatchValue(value=source))])


def _ensure_file_path_index(client, coll_name: str) -> None:
    """Idempotently create a keyword payload index on file_path to speed the focus filter.

    Fail-open: an existing index, a missing collection, or an older server all just mean
    the filter runs unindexed (correct, slower) — never an error to the caller.
    """
    try:
        client.create_payload_index(
            collection_name=coll_name,
            field_name="file_path",
            field_schema=qmodels.PayloadSchemaType.KEYWORD,
        )
    except Exception:
        pass


_FOCUS_RENDER_DPI = 150  # page-image resolution for focus visual hits


def render_page_png(abs_file_path: Path, page: int, repo_root: Path,
                    embed_cfg: dict | None = None) -> bytes | None:
    """Return PNG bytes for a 1-indexed PDF page, or None if it can't be produced.

    Fast path: a ColPali cache PNG under the configured colpali_sidecar_path
    (default .carta/visual_cache/), at <cache_dir>/<stem>/page_NNNN.png.
    Fallback: render on demand with PyMuPDF. Non-PDF, out-of-range page, or any
    failure returns None so the caller degrades to anchors-only for that hit.
    """
    try:
        if abs_file_path.suffix.lower() != ".pdf":
            return None
        cache_dir = Path((embed_cfg or {}).get("colpali_sidecar_path", ".carta/visual_cache/"))
        if not cache_dir.is_absolute():
            cache_dir = repo_root / cache_dir
        cached = cache_dir / abs_file_path.stem / f"page_{page:04d}.png"
        if cached.is_file():
            return cached.read_bytes()
        import fitz  # PyMuPDF, imported lazily
        with fitz.open(str(abs_file_path)) as doc:
            if page < 1 or page > len(doc):
                return None
            pix = doc[page - 1].get_pixmap(dpi=_FOCUS_RENDER_DPI)
            return pix.tobytes("png")
    except Exception:
        return None


def _attach_page_images(hits: list[dict], abs_source_path: Path, repo_root: Path,
                        embed_cfg: dict | None = None) -> list[dict]:
    """Attach a base64 page PNG to hits worth verifying against the page: ColPali visual
    hits and doubted ocr_visual (diagram-OCR) text hits. Mutates + returns hits."""
    import base64
    for hit in hits:
        wants_image = hit.get("type") == "visual" or hit.get("text_source") == "ocr_visual"
        if wants_image and hit.get("page"):
            png = render_page_png(abs_source_path, hit["page"], repo_root, embed_cfg)
            if png is not None:
                hit["image_b64"] = base64.b64encode(png).decode("ascii")
    return hits


def _text_source(payload: dict) -> str:
    """Classify a hit's provenance from existing payload fields.

    The tier reflects TRANSCRIPTION vs INTERPRETATION: glm-ocr transcribes visible text
    (reliable, no fabrication) on both structured-text and flattened/scanned pages, so all
    glm-ocr output is "ocr_table" (trusted); llava *describes/infers* diagrams, so it is
    "ocr_visual" (doubted). "text_layer" is real PDF text (trusted). An unmarked
    image_description chunk defaults to ocr_visual because the embed pipeline's legacy
    model_used fallback is llava.
    """
    if payload.get("doc_type") != "image_description":
        return "text_layer"
    model = (payload.get("model_used") or "").lower()
    content = (payload.get("content_type") or "").lower()
    if "glm" in model or content == "structured_text":
        return "ocr_table"
    return "ocr_visual"


def _text_hit(payload: dict, score: float, lane_ranks: dict | None) -> dict:
    """Build a text-collection hit dict from a Qdrant point's payload.

    Shared by the hybrid (per-lane ranks known) and non-hybrid/legacy
    (``lane_ranks=None``) branches in both ``run_search`` and ``_focus_deep``,
    so the file_path/slug fallback, the page/page_num fallback, and the
    `_text_source` classification stay in lockstep across all four call sites
    instead of drifting independently.
    """
    return {
        "score": score,
        "lane_ranks": lane_ranks,
        "source": payload.get("file_path", payload.get("slug", "")),
        "excerpt": payload.get("text", ""),
        "type": "text",
        "doc_type": payload.get("doc_type", ""),
        "page": payload.get("page") or payload.get("page_num"),
        "section_heading": payload.get("section_heading", ""),
        "text_source": _text_source(payload),
    }


def _focus_outline(client, collections: list[str], ff: Filter, source: str) -> list[dict]:
    """Return the file's distinct (section_heading, page) rows in page order — a synthetic TOC.

    Scrolls text-collection payloads only (no embedding); pages with no number sort last.
    """
    seen: set = set()
    rows: list[tuple] = []
    for coll in collections:
        if coll.endswith("_visual"):
            continue
        try:
            points, _ = client.scroll(
                collection_name=coll, scroll_filter=ff,
                with_payload=True, limit=_FOCUS_OUTLINE_SCROLL_LIMIT,
            )
        except Exception as e:
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str or "doesn't exist" in err_str:
                continue
            if any(kw in err_str for kw in ("connection refused", "connection error",
                                            "network", "timeout", "unreachable")):
                raise RuntimeError(
                    f"Cannot reach Qdrant — is it running? "
                    f"Start it with: carta doctor --fix\n(Detail: {e})") from e
            continue
        for p in points:
            payload = p.payload or {}
            page = payload.get("page")
            heading = payload.get("section_heading", "")
            key = (page, heading)
            if key in seen:
                continue
            seen.add(key)
            sort_page = page if isinstance(page, int) else 1_000_000
            rows.append((sort_page, page, heading))
    rows.sort(key=lambda r: r[0])
    return [{"score": 0.0, "source": source, "page": page,
             "section_heading": heading, "excerpt": "", "type": "outline",
             "doc_type": ""} for _sort, page, heading in rows]


def _embed_query_or_raise(query: str, cfg: dict, collections: list[str],
                          timeout: float | None = None) -> list[float] | None:
    """Embed the text query ONCE, before any per-collection loop.

    Returns the query vector, or None when there are no text collections to search
    (visual-only). A failure raises an actionable RuntimeError rather than being
    swallowed by the per-collection handler as a missing collection — otherwise a
    dead/misconfigured Ollama backend is reported as "no results / nothing
    embedded", sending the user to re-embed a healthy corpus (#79).

    ``timeout`` is an optional per-request budget from a caller on a latency-critical
    path (issue #106). When None the kwarg is omitted entirely rather than forwarded
    as None — requests treats ``timeout=None`` as "wait forever", so passing it
    through would silently REMOVE get_embedding's 60s ceiling instead of keeping it.
    """
    if not any(not c.endswith("_visual") for c in collections):
        return None
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["embed"]["ollama_model"]
    kwargs = {"ollama_url": ollama_url, "model": model, "prefix": "search_query: "}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        return get_embedding(query, **kwargs)
    except Exception as e:
        raise RuntimeError(
            f"Could not embed the query — is Ollama running at {ollama_url} and the "
            f"'{model}' model pulled? Run: carta doctor\n(Detail: {e})"
        ) from e


def _focus_deep(client, collections: list[str], ff: Filter, query: str,
                cfg: dict, repo_root: Path, source: str, limit: int) -> list[dict]:
    """File-scoped deep retrieval: filtered per-collection queries, RRF fused, NO dedup,
    NO visual cap, NO graph expansion; visual hits get a rendered page image.

    Raises RuntimeError if the text query cannot be embedded (Ollama down/misconfigured)
    so the failure is visible rather than silently degrading to an empty result set (#79).
    """
    per_collection: list[list[dict]] = []
    text_query_vec = _embed_query_or_raise(query, cfg, collections)
    for coll_name in collections:
        coll_results: list[dict] = []
        try:
            if coll_name.endswith("_visual"):
                embed_cfg = cfg.get("embed", {})
                if embed_cfg.get("colpali_enabled", None) is False:
                    continue
                from carta.embed.colpali import is_colpali_available, ColPaliEmbedder
                if not is_colpali_available():
                    continue
                if not _visual_collection_ready(client, coll_name):
                    continue
                try:
                    embedder = ColPaliEmbedder(
                        model_name=embed_cfg.get("colpali_model", "vidore/colqwen2-v1.0-hf"),
                        device=embed_cfg.get("colpali_device", "cpu"), batch_size=1)
                    qv = embedder.embed_query(query)
                    qv = qv.tolist() if hasattr(qv, "tolist") else list(qv)
                    response = client.query_points(
                        collection_name=coll_name, query=qv, using="colpali",
                        limit=limit, with_payload=True, query_filter=ff)
                    for r in response.points:
                        payload = r.payload or {}
                        coll_results.append({
                            "score": r.score,
                            "source": f"{payload.get('file_path', payload.get('slug', ''))} (page {payload.get('page_num', '?')})",
                            "excerpt": f"[Visual result] Page {payload.get('page_num', '?')} - {payload.get('file_path', '')}",
                            "type": "visual", "doc_type": payload.get("doc_type", ""),
                            "page": payload.get("page_num"), "section_heading": "",
                            "text_source": "visual"})
                except Exception:
                    pass  # visual lane is auxiliary — skip on any ColPali/query error, keep text results
            else:
                query_vec = text_query_vec  # embedded once up-front (#79)
                hybrid_cfg = cfg.get("search", {}).get("hybrid", {})
                is_hybrid = collection_is_hybrid(client, coll_name)
                if hybrid_cfg.get("enabled", False) and is_hybrid:
                    fused = _hybrid_query_collection(
                        client, coll_name, query, query_vec, limit,
                        prefetch_limit=hybrid_cfg.get("prefetch_limit", 40),
                        bm25_model=hybrid_cfg.get("bm25_model", "Qdrant/bm25"),
                        query_filter=ff,
                        rrf_k=hybrid_cfg.get("rrf_k", 2))
                    for entry in fused:
                        payload = entry["point"].payload or {}
                        coll_results.append(_text_hit(payload, entry["score"], entry["ranks"]))
                else:
                    if is_hybrid:
                        response = client.query_points(
                            collection_name=coll_name, query=query_vec, using=DENSE_VECTOR_NAME,
                            limit=limit, with_payload=True, query_filter=ff)
                    else:
                        response = client.query_points(
                            collection_name=coll_name, query=query_vec,
                            limit=limit, with_payload=True, query_filter=ff)
                    for r in response.points:
                        payload = r.payload or {}
                        coll_results.append(_text_hit(payload, r.score, None))
            per_collection.append(coll_results)
        except Exception as e:
            err_str = str(e).lower()
            if "404" in err_str or "not found" in err_str or "doesn't exist" in err_str:
                continue
            if any(kw in err_str for kw in ("connection refused", "connection error",
                                            "network", "timeout", "unreachable")):
                raise RuntimeError(
                    f"Cannot reach Qdrant — is it running? "
                    f"Start it with: carta doctor --fix\n(Detail: {e})") from e
            continue

    # RRF fuse across lanes; visual_max_ratio=1.0 disables the cap (we WANT the file's pages).
    fused = _rrf_merge_collections(per_collection, limit, visual_max_ratio=1.0)
    # embed_cfg threaded so render_page_png honors a custom colpali_sidecar_path.
    fused = _attach_page_images(fused, repo_root / source, repo_root, cfg.get("embed", {}))
    return fused[:limit]


def run_focus(source: str, cfg: dict, *, query: str = "",
              limit: int = _FOCUS_DEFAULT_LIMIT) -> list[dict]:
    """Deep, file-scoped retrieval over a single source file.

    Modes:
      - query == "" : outline — the file's distinct (section_heading, page) rows in page order.
      - query set   : deep — up to `limit` page-anchored passages from the file (dedup off,
                      no graph expansion, visual cap off); visual hits carry image_b64.

    Returns list of dicts: {score, source, page, section_heading, excerpt, type, doc_type, image_b64?}.
    Fail-open: an unknown/never-embedded file yields []. Raises RuntimeError only on Qdrant transport failure.
    """
    from carta.search.scoped import get_search_collections

    source = _normalize_source(source)
    repo_root = Path(find_config()).parent.parent
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e

    try:
        collections = get_search_collections(cfg, "repo")
    except ValueError:
        collections = [collection_name(cfg, "doc")]
        if cfg.get("embed", {}).get("colpali_enabled", None) is not False:
            collections.append(f"{cfg['project_name']}_visual")

    ff = _file_filter(source)
    for coll in collections:
        _ensure_file_path_index(client, coll)

    if not query:
        return _focus_outline(client, collections, ff, source)
    return _focus_deep(client, collections, ff, query, cfg, repo_root, source, limit)


def _dedupe_by_source(results: list[dict]) -> list[dict]:
    """Keep the first (best-ranked) occurrence of each distinct ``source``, drop the rest.

    Order preserved. Hits without a ``source`` are passed through (treated as distinct),
    so a missing key never collapses unrelated results.
    """
    seen: set = set()
    out: list[dict] = []
    for hit in results:
        src = hit.get("source")
        if src is None:
            out.append(hit)
            continue
        if src in seen:
            continue
        seen.add(src)
        out.append(hit)
    return out


def _rrf_merge_collections(
    per_collection: list[list[dict]],
    top_n: int,
    k: int = 60,
    visual_max_ratio: float = 1.0,
) -> list[dict]:
    """Fuse ranked hit lists from multiple collections with Reciprocal Rank Fusion.

    Each collection's native scores live on incomparable scales — text uses cosine
    or RRF (~0-1) while the visual collection uses ColPali MaxSim (a sum over query
    tokens, ~10-40).  Merging by raw score lets visual hits crowd out every text
    hit.  RRF discards score magnitude and fuses by rank instead, so a rank-0 text
    hit and a rank-0 visual hit compete fairly regardless of scale.

    RRF alone, however, interleaves text and visual ~1:1 by rank, so once a `_visual`
    collection has hits ~half of every fused pool is visual — even for pure-text
    questions — halving effective text depth.  `visual_max_ratio` caps the visual
    lane's share of the returned pool: visual hits beyond the cap are dropped and the
    freed slots are backfilled with deeper text (or, if text is exhausted, restored
    from the diverted visual).  RRF order is preserved among everything admitted.

    Args:
        per_collection: one list per collection, each already ordered best-first.
        top_n: number of fused results to return.
        k: RRF damping constant (Qdrant's fusion default is 60).
        visual_max_ratio: ceiling on the visual lane's share of the pool, as a
            fraction of `top_n` (cap = round(visual_max_ratio * top_n)). 1.0 (default)
            disables the cap; a corpus with no visual hits is unaffected either way.

    Returns:
        Flat list of the original hit dicts (mutated in place), best-first by RRF,
        length <= top_n. Ties (same rank across collections) break toward earlier
        collections, so callers should pass the text collection before the visual
        one. Each hit gains `fused_score` (the RRF value that decided its order)
        and `fused_rank` (its 0-based position in the fused order, assigned before
        the visual cap runs — so a hit dropped by the cap never appears, but a hit
        admitted via cap backfill keeps its pre-cap rank rather than its final
        list position). The pre-existing `score` (intra-collection) is untouched.
    """
    scored = []
    for coll_index, hits in enumerate(per_collection):
        for rank, hit in enumerate(hits):
            rrf = 1.0 / (k + rank + 1)
            scored.append((rrf, coll_index, rank, hit))
    # -rrf: higher fused score first. coll_index/rank: deterministic, text-first ties.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    ordered = []
    for fused_rank, (rrf, _coll_index, _rank, hit) in enumerate(scored):
        # Record the score that actually determined ordering. `score` keeps the
        # intra-collection value; consumers that need ranking magnitude read
        # fused_score. Gate and trace must agree on which number is which.
        hit["fused_score"] = rrf
        hit["fused_rank"] = fused_rank
        ordered.append(hit)
    return _apply_visual_cap(ordered, top_n, visual_max_ratio)


def _apply_graph_expansion(results: list[dict], cfg: dict, repo_root) -> list[dict]:
    """Promote related:-graph neighbours of the top seeds into the candidate pool.

    Undirected 1-hop expansion from the top `seed_count` fused hits; neighbour hits are
    moved to just-after the seeds so a downstream reranker can float them up. Fail-open:
    any error (or graph disabled / no neighbours) returns `results` unchanged.
    """
    graph_cfg = cfg.get("search", {}).get("graph", {})
    if not graph_cfg.get("enabled", False) or not results:
        return results
    try:
        from carta.search.graph import build_related_graph, expand_seeds, promote_graph_neighbors, hit_path
        from pathlib import Path

        seed_count = graph_cfg.get("seed_count", 10)
        graph = build_related_graph(Path(repo_root))
        seeds = [hit_path(h) for h in results[:seed_count]]
        neighbours = expand_seeds(seeds, graph, graph_cfg.get("hops", 1))
        if not neighbours:
            return results
        return promote_graph_neighbors(results, neighbours, seed_count)
    except Exception as exc:
        import sys
        print(f"Warning: graph expansion failed, skipping: {exc}", file=sys.stderr, flush=True)
        return results


def run_search(query: str, cfg: dict, verbose: bool = False, stats: dict | None = None,
               timeout_s: float | None = None) -> list[dict]:
    """Search both text and visual collections for results matching query.

    Args:
        query: natural-language search query.
        cfg: carta config dict.
        verbose: unused, kept for interface consistency.
        stats: optional dict; when provided, run_search records "rerank_requested" and
            "rerank_applied" (rerank_score observed on hits before stripping).
        timeout_s: optional WALL-CLOCK budget for the whole search (issue #106).
            None — the default and what every caller but the hook passes — leaves
            behaviour exactly as it was: a 60s query embed and a 10s Qdrant client,
            with no deadline checks.

    Returns:
        List of dicts: {"score": float, "source": str, "excerpt": str}
        Ordered by descending similarity score.
    """
    from carta.search.scoped import get_search_collections
    from pathlib import Path

    # Wall-clock budget (issue #106). A per-call timeout is NOT a bound: the same
    # 3s applied to one embed and N collections is 3s x (1+N). The proactive-recall
    # hook blocks prompt submission, so it needs a limit it can actually reason
    # about — hence one deadline for the whole call rather than per-request values.
    deadline = (time.monotonic() + timeout_s) if timeout_s else None

    def _remaining() -> float | None:
        """Seconds left in the budget, or None when unbudgeted.

        Floored at 0.1 so an already-spent budget never passes 0 or a negative to
        requests/QdrantClient (where those mean 'no timeout' or raise).
        """
        if deadline is None:
            return None
        return max(0.1, deadline - time.monotonic())

    top_n = cfg.get("search", {}).get("top_n", 5)
    # find_config() returns <repo>/.carta/config.yaml; the repo root is its
    # GRANDPARENT. (.parent alone is the .carta dir, which holds no project docs —
    # that made graph expansion a silent no-op, audit CA-23.)
    repo_root = Path(find_config()).parent.parent

    # Compute effective retrieval depth.
    # When reranking is enabled, fetch candidate_pool docs per collection so
    # the cross-encoder has a wide enough pool to promote lower-ranked relevant
    # documents.  When reranking is off, fetch exactly top_n (unchanged).
    rr_cfg = cfg.get("search", {}).get("rerank", {})
    rerank_enabled = rr_cfg.get("enabled", False)
    candidate_pool = rr_cfg.get("candidate_pool", 30)
    graph_cfg = cfg.get("search", {}).get("graph", {})
    graph_enabled = graph_cfg.get("enabled", False)
    candidate_depth = graph_cfg.get("candidate_depth", 50)
    # Fetch deep enough for the rerank pool AND graph promotion (whichever is wider).
    dedupe_results = cfg.get("search", {}).get("dedupe_results", True)
    fetch_limit = top_n
    if rerank_enabled:
        fetch_limit = max(fetch_limit, candidate_pool)
    if graph_enabled:
        fetch_limit = max(fetch_limit, candidate_depth)
    if dedupe_results:
        # Dedup collapses duplicate chunks/visual pages; fetch a real pool so the
        # top_n SHOWN results are distinct docs, not 2-3 docs' worth of dup chunks.
        fetch_limit = max(fetch_limit, _RESULT_POOL_FLOOR)

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
    
    # Embed the text query ONCE, before the per-collection loop. A failure here is a
    # real embedding-backend outage (Ollama down, wrong/missing model, empty vector),
    # not a per-collection miss — surface it instead of letting the loop's handler
    # mis-classify it and return [] ("nothing embedded", #79).
    text_query_vec = _embed_query_or_raise(query, cfg, collections, timeout=_remaining())

    # Constructed AFTER the embed so a budgeted caller's client reflects the time
    # already spent rather than getting a fresh full budget. Safe to reorder:
    # QdrantClient does not connect on construction, so this try/except never
    # actually fired on a dead backend — the failure surfaces at query time.
    try:
        client = QdrantClient(url=cfg["qdrant_url"], timeout=_remaining() or 10)
    except Exception as e:
        raise RuntimeError(f"Cannot connect to Qdrant: {e}") from e

    # Search each collection independently, then fuse across them by rank (RRF)
    # so incomparable score scales (text cosine/RRF vs visual ColPali MaxSim)
    # can't crowd each other out.
    per_collection: list[list[dict]] = []

    for coll_name in collections:
        if deadline is not None and time.monotonic() >= deadline:
            # Budget spent. Return what we have rather than raising — the hook's
            # noise gate exits silently on an empty result set, so an outage
            # degrades to silence instead of a stderr line on every prompt.
            break
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
                            "doc_type": payload.get("doc_type", ""),
                            "page": payload.get("page_num"),
                            "section_heading": "",
                            "text_source": "visual",
                        })
                        
                except Exception:
                    # Skip visual search on error
                    pass
            else:
                # Text collection search using the query vector embedded once up-front (#79).
                query_vec = text_query_vec

                hybrid_cfg = cfg.get("search", {}).get("hybrid", {})
                is_hybrid = collection_is_hybrid(client, coll_name)

                if hybrid_cfg.get("enabled", False) and is_hybrid:
                    # Hybrid BM25+dense, fused client-side (k=2) so per-lane ranks
                    # are available. Pass fetch_limit as the fusion top_n so the
                    # reranker receives a full candidate_pool rather than just top_n.
                    fused = _hybrid_query_collection(
                        client, coll_name, query, query_vec, fetch_limit,
                        prefetch_limit=hybrid_cfg.get("prefetch_limit", 40),
                        bm25_model=hybrid_cfg.get("bm25_model", "Qdrant/bm25"),
                        rrf_k=hybrid_cfg.get("rrf_k", 2),
                    )
                    for entry in fused:
                        payload = entry["point"].payload or {}
                        coll_results.append(_text_hit(payload, entry["score"], entry["ranks"]))
                else:
                    if is_hybrid:
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
                        coll_results.append(_text_hit(payload, r.score, None))

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
    # visual_max_ratio caps the visual lane's share so text questions keep their depth.
    fusion_cfg = cfg.get("search", {}).get("fusion", {})
    visual_max_ratio = fusion_cfg.get("visual_max_ratio", 1.0)
    # When deduping, defer the visual cap to the final stage (applied against top_n
    # after dedup) so the deepened pool can't let visual over-inject. 1.0 = no cap.
    merge_ratio = 1.0 if dedupe_results else visual_max_ratio
    all_results = _rrf_merge_collections(
        per_collection, fetch_limit, visual_max_ratio=merge_ratio
    )

    # Graph-aware expansion: promote related:-adjacent docs into the pool the reranker
    # sees. Fail-open. Off by default (opt in via search.graph.enabled); when enabled,
    # candidate_depth (default 50) widens the Qdrant fetch to seed the walk.
    if graph_enabled:
        all_results = _apply_graph_expansion(all_results, cfg, repo_root)

    # De-duplicate by source so the reranker ranks distinct docs and the shown
    # top_n covers distinct docs (not duplicate chunks of the same one).
    if dedupe_results:
        all_results = _dedupe_by_source(all_results)

    # Optional second-stage cross-encoder reranking (opt-in via search.rerank.enabled)
    rerank_applied = False
    if rerank_enabled and all_results:
        from carta.search.rerank import rerank_dispatch
        pool = all_results[:candidate_pool]
        # rerank_hits reads chunk text from key "text"; run_search stores it as "excerpt"
        for h in pool:
            h["text"] = h.get("excerpt", "")
        all_results = rerank_dispatch(
            query,
            pool,
            rr_cfg=rr_cfg,
            ollama_url=cfg.get("embed", {}).get("ollama_url", "http://localhost:11434"),
            top_n=top_n,
        )
        # Both backends stamp rerank_score only when they actually ran; every
        # fail-open path returns unstamped hits. Capture the signal before
        # stripping so callers (eval) can detect a silent fail-open.
        rerank_applied = any("rerank_score" in h for h in all_results)
        # Strip transient keys so returned dicts have a stable shape
        # regardless of whether reranking ran.
        for _h in all_results:
            _h.pop("text", None)
            _h.pop("rerank_score", None)

    if stats is not None:
        stats["rerank_requested"] = rerank_enabled
        stats["rerank_applied"] = rerank_applied

    # Cap the visual lane's share of the SHOWN results (relative to top_n, not the
    # deepened fetch pool), preserving the #36 balance after dedup.
    if dedupe_results:
        all_results = _apply_visual_cap(all_results, top_n, visual_max_ratio)

    return all_results[:top_n]
