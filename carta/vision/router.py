"""Smart vision router for PDF page extraction.

Routes each PDF page to the appropriate extraction strategy based on
PageAnalyzer classification. PURE_TEXT pages produce zero model calls.
"""
import base64
import sys
from pathlib import Path
from typing import Any, Optional

import requests

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore

from carta.vision.classifier import PageAnalyzer, PageClass, PageProfile


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
    "Describe this technical diagram for engineering documentation search. "
    "Include: data values, axis labels, register names, waveform descriptions, "
    "block labels, pin names, and any visible technical annotations."
)


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
        self.vision_model: str = embed.get("ollama_vision_model", "llava:latest")
        self.ollama_url: str = embed.get("ollama_url", "http://localhost:11434")
        self.flattened_min_yield: int = embed.get("vision_flattened_min_yield", 50)
        self.max_images_per_page: int = embed.get("vision_max_images_per_page", 4)
        self.analyzer = PageAnalyzer(cfg)

    def extract_pdf(
        self,
        pdf_path: Path,
        progress_callback: Optional[Any] = None,
    ) -> list[dict]:
        """Extract vision chunks from all pages of a PDF.

        Args:
            pdf_path: Path to PDF file.
            progress_callback: Optional callback(page_num, total_pages).

        Returns:
            List of chunk dicts compatible with pipeline.py expectations.
        """
        if fitz is None:
            raise RuntimeError("PyMuPDF (fitz) not available")
        try:
            doc = fitz.open(str(pdf_path))
        except Exception as exc:
            print(
                f"Warning: could not open PDF {pdf_path}: {exc}",
                file=sys.stderr, flush=True,
            )
            return []

        results = []
        total_pages = len(doc)
        for page_num, page in enumerate(doc, start=1):
            if progress_callback:
                try:
                    progress_callback(page_num, total_pages)
                except Exception:
                    pass
            profile = self.analyzer.analyze(page)
            chunks = self._route(page, page_num, profile, doc)
            results.extend(chunks)
        doc.close()
        return results

    def _route(
        self, page: Any, page_num: int, profile: PageProfile, doc: Any
    ) -> list[dict]:
        if profile.page_class == PageClass.PURE_TEXT:
            return []
        if profile.page_class == PageClass.STRUCTURED_TEXT:
            return self._route_structured(page, page_num)
        if profile.page_class == PageClass.TEXT_WITH_IMAGES:
            return self._route_text_with_images(page, page_num, profile, doc)
        return self._route_flattened(page, page_num)

    def _route_structured(self, page: Any, page_num: int) -> list[dict]:
        pix = page.get_pixmap(dpi=150)
        png_bytes = pix.tobytes("png")
        try:
            text = self._call_ollama_vision(
                png_bytes, model=self.ocr_model, prompt=GLM_OCR_PROMPT
            )
        except Exception as exc:
            print(
                f"Warning: GLM-OCR failed for page {page_num}: {exc}",
                file=sys.stderr, flush=True,
            )
            return []
        return [self._make_chunk(page_num, 0, text, "glm-ocr", "structured_text")]

    def _route_text_with_images(
        self, page: Any, page_num: int, profile: PageProfile, doc: Any
    ) -> list[dict]:
        crops = self._extract_image_crops(page, doc)
        chunks = []
        if crops:
            for idx, png_bytes in crops:
                try:
                    text = self._call_ollama_vision(
                        png_bytes, model=self.vision_model, prompt=LLAVA_PROMPT
                    )
                    chunks.append(
                        self._make_chunk(page_num, idx, text, "llava", "text_with_images")
                    )
                except Exception as exc:
                    print(
                        f"Warning: LLaVA failed page {page_num} image {idx}: {exc}",
                        file=sys.stderr, flush=True,
                    )
        else:
            # Caption fallback: likely a vector graphic not listed by get_images()
            pix = page.get_pixmap(dpi=150)
            png_bytes = pix.tobytes("png")
            try:
                text = self._call_ollama_vision(
                    png_bytes, model=self.vision_model, prompt=LLAVA_PROMPT
                )
                chunks.append(
                    self._make_chunk(page_num, 0, text, "llava", "text_with_images")
                )
            except Exception as exc:
                print(
                    f"Warning: LLaVA failed for page {page_num}: {exc}",
                    file=sys.stderr, flush=True,
                )
        return chunks

    def _route_flattened(self, page: Any, page_num: int) -> list[dict]:
        pix = page.get_pixmap(dpi=150)
        png_bytes = pix.tobytes("png")
        try:
            ocr_text = self._call_ollama_vision(
                png_bytes, model=self.ocr_model, prompt=GLM_OCR_PROMPT
            )
        except Exception as exc:
            print(
                f"Warning: GLM-OCR failed for flattened page {page_num}: {exc}",
                file=sys.stderr, flush=True,
            )
            return []
        if len(ocr_text) >= self.flattened_min_yield:
            return [self._make_chunk(page_num, 0, ocr_text, "glm-ocr", "flattened")]
        # Low yield — page is likely a photo or decorative image, try LLaVA
        try:
            vision_text = self._call_ollama_vision(
                png_bytes, model=self.vision_model, prompt=LLAVA_PROMPT
            )
            return [self._make_chunk(page_num, 0, vision_text, "llava", "flattened")]
        except Exception as exc:
            print(
                f"Warning: LLaVA fallback failed for flattened page {page_num}: {exc}",
                file=sys.stderr, flush=True,
            )
            # Return the low-yield OCR result rather than discarding it
            return [self._make_chunk(page_num, 0, ocr_text, "glm-ocr", "flattened")]

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
        timeout: int = 120,
    ) -> str:
        """Call Ollama vision API. Raises RuntimeError on non-200 response."""
        b64 = base64.b64encode(image_png_bytes).decode("utf-8")
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "images": [b64], "stream": False},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ollama returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()["response"].strip()


def extract_image_descriptions_intelligent(
    pdf_path: Path,
    cfg: dict,
    progress_callback: Optional[Any] = None,
) -> list[dict]:
    """Extract image descriptions from PDF using smart page routing.

    Drop-in replacement for the previous DualExtractionRouter-based function.
    PURE_TEXT pages produce zero Ollama calls.

    Args:
        pdf_path: Path to PDF file.
        cfg: Carta config dict.
        progress_callback: Optional callback(page_num, total_pages).

    Returns:
        List of dicts with keys: doc_type, page_num, image_index, text,
        model_used, page_class.
    """
    router = SmartRouter(cfg)
    return router.extract_pdf(pdf_path, progress_callback)
