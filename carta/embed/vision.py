"""Vision module for carta: extract images from PDFs and describe them via Ollama."""

import base64
import sys
import requests
from pathlib import Path

try:
    import fitz  # PyMuPDF — optional at import time; patched in tests
except ImportError:
    fitz = None  # type: ignore

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

VISION_PROMPT = (
    "Extract all technical data from this image: data values, axis labels and units, "
    "register field names and bit widths, table cell contents, pin names, "
    "waveform annotations, block labels. List each item."
)
# <=200 chars per D-07


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _convert_to_rgb_png(pix, fitz) -> bytes:
    """Convert a PyMuPDF Pixmap to RGB PNG bytes.

    If the colorspace is not RGB or grayscale (e.g. CMYK), convert to RGB first
    to avoid RuntimeError on PNG encoding of CMYK images.

    Args:
        pix: fitz.Pixmap object.
        fitz: the fitz module (passed to avoid re-import).

    Returns:
        PNG bytes.
    """
    if pix.colorspace not in (fitz.csGRAY, fitz.csRGB):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return pix.tobytes("png")


def _scale_pixmap_if_needed(doc, page, xref, pix, fitz):
    """Scale down a Pixmap if either dimension exceeds 2048px.

    Uses page.get_pixmap() with a scaling matrix to re-extract at reduced size.
    Falls back to original pix if bounding rect is unavailable.

    Args:
        doc: fitz.Document (unused but kept for API symmetry).
        page: fitz.Page containing the image.
        xref: xref integer for the image.
        pix: original fitz.Pixmap.
        fitz: the fitz module.

    Returns:
        fitz.Pixmap (possibly re-extracted at smaller size).
    """
    if pix.width <= 2048 and pix.height <= 2048:
        return pix

    scale = 2048 / max(pix.width, pix.height)
    try:
        rects = page.get_image_rects(xref)
        if not rects:
            return pix
        img_bbox = rects[0]
        matrix = fitz.Matrix(scale, scale)
        return page.get_pixmap(clip=img_bbox, matrix=matrix)
    except Exception as exc:
        print(
            f"Warning: could not scale image xref={xref}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return pix


def _extract_embedded_images(doc, page, fitz) -> list:
    """Extract all embedded image objects from a page as PNG bytes.

    Args:
        doc: fitz.Document.
        page: fitz.Page.
        fitz: the fitz module.

    Returns:
        List of PNG bytes, one per successfully extracted image.
    """
    png_list = []
    for img_tuple in page.get_images(full=True):
        xref = img_tuple[0]
        try:
            pix = fitz.Pixmap(doc, xref)
            pix = _scale_pixmap_if_needed(doc, page, xref, pix, fitz)
            png_bytes = _convert_to_rgb_png(pix, fitz)
            png_list.append(png_bytes)
        except Exception as exc:
            print(
                f"Warning: failed to extract image xref={xref}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    return png_list


def _has_significant_vector_content(page) -> bool:
    """Return True if the page has >= 3 vector drawings (threshold to skip trivial borders).

    Per Research Pitfall 6: a single rectangle is often just a page border; >=3 indicates
    a real diagram (chart axes, register map cells, etc.).

    Args:
        page: fitz.Page.

    Returns:
        bool.
    """
    return len(page.get_drawings()) >= 3


def _render_page_as_png(page, dpi: int = 150) -> bytes:
    """Render an entire page to PNG bytes at the given DPI.

    Used as fallback for pages that contain vector drawings but no embedded images.

    Args:
        page: fitz.Page.
        dpi: render resolution (default 150 per D-05).

    Returns:
        PNG bytes.
    """
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


def _call_vision_model(
    image_png_bytes: bytes,
    ollama_url: str,
    model: str,
    timeout: int = 60,
) -> str:
    """Send a PNG image to an Ollama vision model and return the description.

    Args:
        image_png_bytes: raw PNG bytes.
        ollama_url: base URL for Ollama API (e.g. "http://localhost:11434").
        model: Ollama model name (e.g. "llava:latest").
        timeout: request timeout in seconds.

    Returns:
        Description string from the model.

    Raises:
        RuntimeError: if Ollama returns a non-200 status.
    """
    b64 = base64.b64encode(image_png_bytes).decode("utf-8")
    resp = requests.post(
        f"{ollama_url}/api/generate",
        json={
            "model": model,
            "prompt": VISION_PROMPT,
            "images": [b64],
            "stream": False,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Ollama vision model returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()["response"].strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_image_descriptions(pdf_path: Path, cfg: dict) -> list[dict]:
    """Extract image descriptions from a PDF using an Ollama vision model.

    For each page:
      1. Extract embedded image objects → call vision model on each.
      2. If no embedded images but >= 3 vector drawings → render page at 150 DPI
         and call vision model on the render.

    Each successful description is returned as a dict with:
      doc_type="image_description", page_num, image_index, text.

    Failure for any single image is logged to stderr and skipped (fail-open per D-11).

    Args:
        pdf_path: path to the PDF file.
        cfg: carta config dict (must contain cfg["embed"]["ollama_url"] and
             cfg["embed"]["ollama_vision_model"]).

    Returns:
        List of image description dicts, empty if no images or all failed.
    """
    ollama_url = cfg["embed"]["ollama_url"]
    model = cfg["embed"].get("ollama_vision_model", "llava:latest")

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        print(
            f"Warning: could not open PDF {pdf_path}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return []

    results = []

    for page_num, page in enumerate(doc, start=1):
        # --- Embedded image objects ---
        embedded_pngs = _extract_embedded_images(doc, page, fitz)
        for img_index, png_bytes in enumerate(embedded_pngs):
            try:
                description = _call_vision_model(png_bytes, ollama_url, model)
                results.append({
                    "page_num": page_num,
                    "image_index": img_index,
                    "doc_type": "image_description",
                    "text": description,
                })
            except Exception as exc:
                print(
                    f"Warning: vision model failed for page {page_num} image {img_index}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

        # --- Page-render fallback for vector-only pages ---
        if not embedded_pngs and _has_significant_vector_content(page):
            try:
                png_bytes = _render_page_as_png(page, dpi=150)
                description = _call_vision_model(png_bytes, ollama_url, model)
                results.append({
                    "page_num": page_num,
                    "image_index": 0,
                    "doc_type": "image_description",
                    "text": description,
                })
            except Exception as exc:
                print(
                    f"Warning: vision model failed for page-render fallback page {page_num}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    doc.close()
    return results
