"""Smart vision router for PDF page extraction.

Routes each PDF page to the appropriate extraction strategy based on
PageAnalyzer classification. PURE_TEXT pages produce zero model calls.
"""
import base64
import concurrent.futures
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore

from carta.mupdf_util import mupdf_quiet
from carta.vision.classifier import PageAnalyzer, PageClass, PageProfile


_CHECKPOINT_SCHEMA_VERSION = 1


def load_vision_checkpoint(
    path: Optional[Path],
    vision_model: str,
    ocr_model: str,
) -> dict[int, list[dict]]:
    """Read completed-page chunks from a checkpoint file.

    Returns ``{page_num: [chunk, ...]}`` for pages already processed in a prior
    run. Returns an empty dict if the file is missing, unreadable, the schema
    version doesn't match, or the configured models have changed since the
    checkpoint was written (in which case the stale checkpoint is removed).
    """
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION:
        return {}
    if data.get("vision_model") != vision_model or data.get("ocr_model") != ocr_model:
        # Model swap invalidates prior partial work.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {}
    out: dict[int, list[dict]] = {}
    for entry in data.get("completed_pages", []):
        page_num = entry.get("page_num")
        chunks = entry.get("chunks", [])
        if isinstance(page_num, int) and isinstance(chunks, list):
            out[page_num] = chunks
    return out


def save_vision_checkpoint(
    path: Optional[Path],
    file_path: Path,
    vision_model: str,
    ocr_model: str,
    completed_chunks: list[dict],
) -> None:
    """Atomically persist the running list of completed-page chunks.

    Writes to a sibling tempfile then renames into place so a partial write
    can never corrupt a checkpoint mid-run. Logging errors (disk full, etc.)
    are intentionally swallowed — checkpointing is best-effort.
    """
    if path is None:
        return
    payload = {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "file_path": str(file_path),
        "vision_model": vision_model,
        "ocr_model": ocr_model,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "completed_pages": completed_chunks,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


GLM_OCR_PROMPT = """Extract all text content from this document page.

If tables are present, format them as markdown tables with proper headers and alignment.
Preserve numerical values, units, and specifications exactly as shown in the original.
Maintain the document's hierarchical structure (headers, lists, paragraphs).

For technical specifications:
- Keep all numbers, units, and tolerances intact
- Preserve register addresses and bit field descriptions
- Maintain pin numbers and signal names exactly

Output only the extracted content with markdown formatting. No explanatory text."""

LLAVA_PROMPT = (
    "Transcribe the text visible in this technical image for documentation search. "
    "List every visible text label, annotation, axis label, value with its unit, pin name, "
    "block label, and reference designator exactly as printed. "
    "Do NOT infer or guess component functions, designators, values, or connections that "
    "are not directly legible as text in the image. If something is unreadable, omit it. "
    "Output only the transcribed items, one per line."
)

DEEP_STRUCTURE_PROMPT = (
    "Describe what this technical drawing shows: name each component, state what "
    "connects to what, and note what sits between which elements. Transcribe labels "
    "verbatim, including non-English text. Do not invent details; omit anything "
    "unreadable."
)


def tile_rects(x0, y0, x1, y1, dpi, tile_px, overlap):
    """Grid of (x0, y0, x1, y1) point-space clips; each renders <= tile_px per edge at dpi.

    Raises:
        ValueError: if tile_px < 1 or overlap is not in [0, 1) — either would
            make the sliding-window step <= 0, looping forever.
    """
    if tile_px < 1:
        raise ValueError(f"tile_px must be >= 1, got {tile_px!r}")
    if not (0 <= overlap < 1.0):
        raise ValueError(f"overlap must be in [0, 1), got {overlap!r}")
    tile_pts = tile_px / (dpi / 72.0)
    if (x1 - x0) <= tile_pts and (y1 - y0) <= tile_pts:
        return [(x0, y0, x1, y1)]
    step = tile_pts * (1.0 - overlap)
    rects = []
    y = y0
    while True:
        x = x0
        while True:
            rects.append((x, y, min(x + tile_pts, x1), min(y + tile_pts, y1)))
            if x + tile_pts >= x1:
                break
            x += step
        if y + tile_pts >= y1:
            break
        y += step
    return rects


class SmartRouter:
    """Routes PDF pages to extraction strategies via PageAnalyzer classification.

    PURE_TEXT pages produce zero model calls. Other classes trigger targeted
    Ollama calls: GLM-OCR for structured text/tables, LLaVA for embedded
    images, and GLM-OCR → LLaVA fallback for flattened pages.

    Args:
        cfg: Carta config dict.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        embed = cfg.get("embed", {})
        self.ocr_model: str = embed.get("ocr_model", "glm-ocr:latest")
        self.vision_model: str = embed.get("ollama_vision_model", "qwen3-vl:8b")
        self.ollama_url: str = embed.get("ollama_url", "http://localhost:11434")
        self.flattened_min_yield: int = embed.get("vision_flattened_min_yield", 50)
        self.max_images_per_page: int = embed.get("vision_max_images_per_page", 4)
        self.vision_workers: int = max(1, int(embed.get("vision_workers", 4)))
        # vision_routing: "auto" | "ocr" | "vision" | "off"
        self.vision_routing: str = embed.get("vision_routing", "auto")
        # Configurable per-call timeout (seconds); replaces the old hardcoded 120
        self.vision_call_timeout: int = int(embed.get("vision_call_timeout_s", 300))
        # Full-page pixmap render DPI; replaces the old hardcoded dpi=150 literals
        # in _route_structured, _route_text_with_images (caption fallback), and
        # _route_flattened (also used by VECTOR_DRAWING, which shares that path).
        self.render_dpi: int = int(embed.get("vision_render_dpi", 150))
        # Deep tiled-extraction config (dpi/tile_px/tile_overlap); see extract_page_deep.
        self.deep_cfg: dict = embed.get("deep_scan", {}) or {}
        self.analyzer = PageAnalyzer(cfg)
        # PyMuPDF (fitz) is not thread-safe. Workers must serialize all fitz
        # operations through this lock; Ollama HTTP calls run unlocked so they
        # can overlap across worker threads.
        self._fitz_lock = threading.Lock()

    def extract_pdf(
        self,
        pdf_path: Path,
        progress_callback: Optional[Any] = None,
        cancel_event: Optional[threading.Event] = None,
        checkpoint_path: Optional[Path] = None,
    ) -> list[dict]:
        """Extract vision chunks from all pages of a PDF.

        Args:
            pdf_path: Path to PDF file.
            progress_callback: Optional callback(page_num, total_pages, page_class, model_used, char_count).
                               page_class: "pure_text"|"structured_text"|"text_with_images"|"flattened"|"vector_drawing"
                               model_used: "skip" for PURE_TEXT, otherwise the model name (e.g. "glm-ocr", "llava")
                               char_count: total chars extracted for this page; 0 for skipped pages.
            checkpoint_path: Optional path to a JSON file persisting per-page progress
                             so a re-run can resume mid-PDF. The file is updated after
                             each page completes; the caller is responsible for deleting
                             it once the file is fully embedded.

        Returns:
            List of chunk dicts compatible with pipeline.py expectations.
        """
        if fitz is None:
            raise RuntimeError("PyMuPDF (fitz) not available")
        with mupdf_quiet():
            try:
                doc = fitz.open(str(pdf_path))
            except Exception as exc:
                print(
                    f"Warning: could not open PDF {pdf_path}: {exc}",
                    file=sys.stderr, flush=True,
                )
                return []

            try:
                total_pages = len(doc)

                # Load resume state. completed[page_num] -> list of chunks already done.
                completed = load_vision_checkpoint(
                    checkpoint_path, self.vision_model, self.ocr_model
                )
                checkpoint_lock = threading.Lock()

                def persist_checkpoint():
                    """Snapshot ``completed`` to disk (caller holds checkpoint_lock)."""
                    save_vision_checkpoint(
                        checkpoint_path,
                        pdf_path,
                        self.vision_model,
                        self.ocr_model,
                        [{"page_num": p, "chunks": completed[p]} for p in sorted(completed)],
                    )

                page_specs: list[tuple[int, Any, PageProfile]] = []
                for page_num, page in enumerate(doc, start=1):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if page_num in completed:
                        # Resumed page — skip fitz analysis too; we already have chunks.
                        continue
                    with self._fitz_lock:
                        profile = self.analyzer.analyze(page)
                    page_specs.append((page_num, page, profile))

                if completed and progress_callback:
                    # Tell the caller about resumed pages so the perf log + UI reflect them.
                    for p in sorted(completed):
                        chunks = completed[p]
                        try:
                            char_count = sum(len(c.get("text", "")) for c in chunks)
                            model_used = chunks[0]["model_used"] if chunks else "skip"
                            page_class = chunks[0].get("page_class", "pure_text") if chunks else "pure_text"
                            progress_callback(p, total_pages, page_class, model_used, char_count)
                        except Exception:
                            pass

                def route_one(spec):
                    page_num, page, profile = spec
                    if cancel_event is not None and cancel_event.is_set():
                        return None
                    chunks = self._route(page, page_num, profile, doc)
                    return page_num, profile, chunks

                def record_page(page_num, profile, chunks):
                    with checkpoint_lock:
                        completed[page_num] = chunks
                        persist_checkpoint()
                    if progress_callback:
                        try:
                            char_count = sum(len(c.get("text", "")) for c in chunks)
                            model_used = chunks[0]["model_used"] if chunks else "skip"
                            page_class = profile.page_class.name.lower()
                            progress_callback(page_num, total_pages, page_class, model_used, char_count)
                        except Exception:
                            pass

                if self.vision_workers <= 1 or len(page_specs) <= 1:
                    for spec in page_specs:
                        result = route_one(spec)
                        if result is None:
                            break
                        p, profile, chunks = result
                        record_page(p, profile, chunks)
                else:
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=self.vision_workers,
                        thread_name_prefix="carta-vision",
                    ) as ex:
                        futures = [ex.submit(route_one, s) for s in page_specs]
                        for fut in concurrent.futures.as_completed(futures):
                            result = fut.result()
                            if result is None:
                                continue
                            p, profile, chunks = result
                            record_page(p, profile, chunks)

                return [c for p in sorted(completed) for c in completed[p]]
            finally:
                doc.close()

    def _route(
        self, page: Any, page_num: int, profile: PageProfile, doc: Any
    ) -> list[dict]:
        # vision_routing override modes — checked before auto dispatch.
        # Every mode below gates only on PURE_TEXT, so VECTOR_DRAWING (never
        # equal to PURE_TEXT) automatically falls through with FLATTENED in
        # each mode — it must never hit a PURE_TEXT early-out. Whichever path
        # it takes, pass the real page_class_str through so the *persisted*
        # chunk (and therefore checkpoint-resume progress reporting, which
        # replays from the chunk — not the live profile) also reports
        # "vector_drawing" rather than whatever string that path's other
        # callers hardcode ("structured_text"/"text_with_images"/"flattened").
        is_vector_drawing = profile.page_class is PageClass.VECTOR_DRAWING
        if self.vision_routing == "off":
            # Never call any model; treat every page as text-only
            return []
        if self.vision_routing == "ocr":
            # Every non-PURE_TEXT page goes through OCR only — never call VLM
            if profile.page_class == PageClass.PURE_TEXT:
                return []
            if is_vector_drawing:
                return self._route_structured(page, page_num, page_class_str=profile.page_class.value)
            return self._route_structured(page, page_num)
        if self.vision_routing == "vision":
            # Every non-PURE_TEXT page goes through VLM only — never call OCR
            if profile.page_class == PageClass.PURE_TEXT:
                return []
            if is_vector_drawing:
                return self._route_text_with_images(
                    page, page_num, profile, doc, page_class_str=profile.page_class.value
                )
            return self._route_text_with_images(page, page_num, profile, doc)
        # mode == "auto" (default): original heuristic dispatch
        if profile.page_class == PageClass.PURE_TEXT:
            return []
        if profile.page_class == PageClass.STRUCTURED_TEXT:
            return self._route_structured(page, page_num)
        if profile.page_class == PageClass.TEXT_WITH_IMAGES:
            return self._route_text_with_images(page, page_num, profile, doc)
        # FLATTENED and VECTOR_DRAWING (raster-free, vector-CAD dense — no
        # embedded images, so it never matches TEXT_WITH_IMAGES above) both
        # fall through to the full-page render + OCR->LLaVA fallback path.
        if is_vector_drawing:
            return self._route_flattened(page, page_num, page_class_str=profile.page_class.value)
        return self._route_flattened(page, page_num)

    def _route_structured(
        self, page: Any, page_num: int, page_class_str: str = "structured_text"
    ) -> list[dict]:
        with self._fitz_lock:
            pix = page.get_pixmap(dpi=self.render_dpi)
            png_bytes = pix.tobytes("png")
        try:
            text = self._call_ollama_vision(
                png_bytes, model=self.ocr_model, prompt=GLM_OCR_PROMPT,
                timeout=self.vision_call_timeout,
            )
        except Exception as exc:
            print(
                f"Warning: {self.ocr_model} failed for page {page_num}: {exc}",
                file=sys.stderr, flush=True,
            )
            return []
        return [self._make_chunk(page_num, 0, text, "glm-ocr", page_class_str)]

    def _route_text_with_images(
        self,
        page: Any,
        page_num: int,
        profile: PageProfile,
        doc: Any,
        page_class_str: str = "text_with_images",
    ) -> list[dict]:
        with self._fitz_lock:
            crops = self._extract_image_crops(page, doc)
        chunks = []
        if crops:
            for idx, png_bytes in crops:
                try:
                    text = self._call_ollama_vision(
                        png_bytes, model=self.vision_model, prompt=LLAVA_PROMPT,
                        timeout=self.vision_call_timeout,
                    )
                    chunks.append(
                        self._make_chunk(page_num, idx, text, "llava", page_class_str)
                    )
                except Exception as exc:
                    print(
                        f"Warning: {self.vision_model} failed page {page_num} image {idx}: {exc}",
                        file=sys.stderr, flush=True,
                    )
        else:
            # Caption fallback: likely a vector graphic not listed by get_images()
            with self._fitz_lock:
                pix = page.get_pixmap(dpi=self.render_dpi)
                png_bytes = pix.tobytes("png")
            try:
                text = self._call_ollama_vision(
                    png_bytes, model=self.vision_model, prompt=LLAVA_PROMPT,
                    timeout=self.vision_call_timeout,
                )
                chunks.append(
                    self._make_chunk(page_num, 0, text, "llava", page_class_str)
                )
            except Exception as exc:
                print(
                    f"Warning: {self.vision_model} failed for page {page_num}: {exc}",
                    file=sys.stderr, flush=True,
                )
        return chunks

    def _route_flattened(
        self, page: Any, page_num: int, page_class_str: str = "flattened"
    ) -> list[dict]:
        with self._fitz_lock:
            pix = page.get_pixmap(dpi=self.render_dpi)
            png_bytes = pix.tobytes("png")
        try:
            ocr_text = self._call_ollama_vision(
                png_bytes, model=self.ocr_model, prompt=GLM_OCR_PROMPT,
                timeout=self.vision_call_timeout,
            )
        except Exception as exc:
            print(
                f"Warning: {self.ocr_model} failed for flattened page {page_num}: {exc}",
                file=sys.stderr, flush=True,
            )
            return []
        if len(ocr_text) >= self.flattened_min_yield:
            return [self._make_chunk(page_num, 0, ocr_text, "glm-ocr", page_class_str)]
        # Low yield — page is likely a photo or decorative image, try LLaVA
        try:
            vision_text = self._call_ollama_vision(
                png_bytes, model=self.vision_model, prompt=LLAVA_PROMPT,
                timeout=self.vision_call_timeout,
            )
            return [self._make_chunk(page_num, 0, vision_text, "llava", page_class_str)]
        except Exception as exc:
            print(
                f"Warning: {self.vision_model} fallback failed for flattened page {page_num}: {exc}",
                file=sys.stderr, flush=True,
            )
            # Return the low-yield OCR result rather than discarding it
            return [self._make_chunk(page_num, 0, ocr_text, "glm-ocr", page_class_str)]

    def extract_page_deep(self, page: Any, page_num: int) -> list[dict]:
        """High-DPI tiled extraction: transcription + structure prompt per tile."""
        dpi = int(self.deep_cfg.get("dpi", 300))
        # Clamp rather than pass through raw config: tile_rects raises on an
        # out-of-range tile_px/overlap (nontermination guard), but a config
        # typo mid-drain should degrade gracefully, not raise and abort the
        # page (the drain's per-page try/except doesn't help against a hang,
        # and aborting on every page for the run's duration is worse than a
        # clamped-but-working tile grid).
        tile_px = max(1, int(self.deep_cfg.get("tile_px", 1280)))
        overlap = min(max(float(self.deep_cfg.get("tile_overlap", 0.15)), 0.0), 0.9)
        import fitz  # lazy

        with self._fitz_lock:
            r = page.rect
            tiles = tile_rects(r.x0, r.y0, r.x1, r.y1, dpi, tile_px, overlap)
        chunks: list[dict] = []
        for t_idx, (tx0, ty0, tx1, ty1) in enumerate(tiles):
            try:
                with self._fitz_lock:
                    pix = page.get_pixmap(dpi=dpi, clip=fitz.Rect(tx0, ty0, tx1, ty1))
                    png = pix.tobytes("png")
            except Exception as exc:  # a failed render degrades, never aborts
                print(
                    f"Warning: deep render failed page {page_num} tile {t_idx}: {exc}",
                    file=sys.stderr, flush=True,
                )
                continue
            for extraction, model, prompt in (
                ("transcription", self.ocr_model, GLM_OCR_PROMPT),
                ("structure", self.vision_model, DEEP_STRUCTURE_PROMPT),
            ):
                try:
                    text = self._call_ollama_vision(
                        png, model=model, prompt=prompt,
                        timeout=self.vision_call_timeout,
                    )
                except Exception as exc:  # a failed tile degrades, never aborts
                    print(
                        f"Warning: deep {extraction} failed page {page_num} "
                        f"tile {t_idx}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    continue
                if not text.strip():
                    continue
                ch = self._make_chunk(page_num, t_idx, text, model, "deep_scan")
                ch["tile"] = t_idx
                ch["extraction"] = extraction
                chunks.append(ch)
        return chunks

    def _extract_image_crops(self, page: Any, doc: Any) -> list[tuple[int, bytes]]:
        """Return (image_index, png_bytes) for embedded images.

        Sorted by bounding box area descending, capped at max_images_per_page.
        Failed extractions are silently skipped.
        """
        images = page.get_images()
        if not images:
            return []

        items: list[tuple[float, int, int]] = []
        for idx, img in enumerate(images):
            xref = img[0]
            area = 0.0
            try:
                rects = page.get_image_rects(xref)
                if rects:
                    r = rects[0]
                    area = abs(r.width * r.height)
            except Exception:
                pass
            items.append((area, xref, idx))

        items.sort(key=lambda x: x[0], reverse=True)
        items = items[: self.max_images_per_page]

        crops: list[tuple[int, bytes]] = []
        for _, xref, idx in items:
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.colorspace not in (fitz.csRGB, fitz.csGRAY):
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                crops.append((idx, pix.tobytes("png")))
            except Exception as exc:
                print(
                    f"Warning: could not extract image xref={xref}: {exc}",
                    file=sys.stderr, flush=True,
                )
        return crops

    def _make_chunk(
        self,
        page_num: int,
        image_index: int,
        text: str,
        model_used: str,
        page_class_str: str,
    ) -> dict:
        return {
            "doc_type": "image_description",
            "page_num": page_num,
            "image_index": image_index,
            "text": text,
            "model_used": model_used,
            "page_class": page_class_str,
            "content_type": page_class_str,  # consumed by pipeline._build_vision_metadata
        }

    def _call_ollama_vision(
        self,
        image_png_bytes: bytes,
        model: str,
        prompt: str,
        timeout: int = 300,
    ) -> str:
        """Call Ollama vision API using streaming to avoid response-length timeouts.

        With stream=True, the timeout applies per-read rather than per-complete-response,
        so dense tables that generate thousands of tokens don't hit the configured timeout wall.
        """
        b64 = base64.b64encode(image_png_bytes).decode("utf-8")
        # Context-manage the streamed response: iter_lines breaks early on the
        # first done:true line, leaving the body undrained — without an explicit
        # close urllib3 will not return the connection to the pool, leaking a
        # socket per page over a long visual drain.
        with requests.post(
            f"{self.ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "images": [b64], "stream": True},
            timeout=timeout,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Ollama returned {resp.status_code}: {resp.text[:200]}"
                )
            parts: list[str] = []
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                parts.append(chunk.get("response", ""))
                if chunk.get("done"):
                    break
            return "".join(parts).strip()


def extract_image_descriptions_intelligent(
    pdf_path: Path,
    cfg: dict,
    progress_callback: Optional[Any] = None,
    cancel_event: Optional[threading.Event] = None,
    checkpoint_path: Optional[Path] = None,
) -> list[dict]:
    """Extract image descriptions from PDF using smart page routing.

    Drop-in replacement for the previous DualExtractionRouter-based function.
    PURE_TEXT pages produce zero Ollama calls.

    Args:
        pdf_path: Path to PDF file.
        cfg: Carta config dict.
        progress_callback: Optional callback(page_num, total_pages, page_class, model_used, char_count).
                           page_class: "pure_text"|"structured_text"|"text_with_images"|"flattened"|"vector_drawing"
                           model_used: "skip" for PURE_TEXT, otherwise the model name (e.g. "glm-ocr", "llava")
                           char_count: total chars extracted for this page; 0 for skipped pages.
        cancel_event: Optional threading.Event; if set, stops page iteration early.
        checkpoint_path: Optional path for per-page resume state (see SmartRouter.extract_pdf).

    Returns:
        List of dicts with keys: doc_type, page_num, image_index, text,
        model_used, page_class.
    """
    router = SmartRouter(cfg)
    return router.extract_pdf(
        pdf_path,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        checkpoint_path=checkpoint_path,
    )
