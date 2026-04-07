# Smart Vision Routing for PDF Embedding

**Date:** 2026-04-05
**Status:** Approved

## Problem

`carta embed` runs vision models (GLM-OCR + LLaVA) on every page of every PDF, regardless of whether the page needs vision processing. For a typical datasheet (50–100 pages, mostly text), this triggers 50–200 sequential Ollama calls and reliably hits the 300s file timeout. The root causes:

1. No per-page skip logic — all pages go through `extract_image_descriptions_intelligent`
2. The existing `ContentClassifier` uses broken image coverage metrics (pixel dimensions divided by PDF point area — different units)
3. `vision_routing: auto` was designed to choose *which* vision model, not *whether* to use one at all

## Goal

Most pages on a datasheet should cost zero vision model calls. Vision should only fire when a page actually needs it: tables that PyMuPDF would scramble, embedded images worth describing, or flattened pages where PyMuPDF extracts nothing.

## Page Classification

Four page classes replace the old TEXT/MIXED/VISUAL taxonomy:


| Class              | Signals                                                                              | Action                                                        |
| ------------------ | ------------------------------------------------------------------------------------ | ------------------------------------------------------------- |
| `PURE_TEXT`        | text ≥ MIN, no images, no tables, no captions                                        | PyMuPDF only — 0 model calls                                  |
| `STRUCTURED_TEXT`  | text ≥ MIN, table patterns detected *(takes priority over images)*                   | GLM-OCR full page — 1 call                                    |
| `TEXT_WITH_IMAGES` | text ≥ MIN, no tables, embedded images present OR (captions detected AND text < MAX) | LLaVA per image crop — pipeline keeps PyMuPDF text separately |
| `FLATTENED`        | text < MIN                                                                           | GLM-OCR full page; if yield < 50 chars → LLaVA full page      |


**Thresholds (all config-tunable):**

- `TEXT_MIN = 150` chars — below this, PyMuPDF extracted nothing useful
- `TEXT_MAX = 600` chars — above this, figure captions are cross-references to other pages, not signals that an image is present on this page
- `FLATTENED_MIN_YIELD = 50` chars — GLM-OCR output below this triggers LLaVA fallback
- `MAX_IMAGES_PER_PAGE = 4` — cap LLaVA calls per page; sort by bounding box area descending, skip smallest

### Figure Caption Detection

When `page.get_images()` returns empty but the page text contains figure references (`Fig.`, `Figure N`, `see plot`, `see chart`, `see diagram`), the image may be a vector graphic not listed by PyMuPDF. This triggers `TEXT_WITH_IMAGES` routing **only if** `text_length < TEXT_MAX`. Above that threshold, the caption is almost certainly a prose cross-reference to a figure on another page — no LLaVA call.

Regex: `r'\b(fig\.?|figure|plot|chart|diagram|graph)\s*\d+\b'` (case-insensitive)

When triggered via caption fallback (no `get_images()` results), the full page is rendered and sent to LLaVA rather than individual crops.

## Module Changes

### `carta/vision/classifier.py` — rewritten

**Removes:** `ContentType` enum, `ContentClassifier`, `ClassificationResult`, PIL/entropy analysis, `classify_image()`.

**Adds:**

```python
class PageClass(Enum):
    PURE_TEXT = "pure_text"
    STRUCTURED_TEXT = "structured_text"
    TEXT_WITH_IMAGES = "text_with_images"
    FLATTENED = "flattened"

@dataclass
class PageProfile:
    text_length: int
    has_images: bool        # page.get_images() non-empty
    has_tables: bool        # column-alignment heuristic
    has_captions: bool      # figure caption regex match
    page_class: PageClass

class PageAnalyzer:
    def __init__(self, cfg: dict): ...
    def analyze(self, page: Any) -> PageProfile: ...
```

`PageAnalyzer.analyze()` makes zero model calls. The column-alignment table heuristic from the old `ContentClassifier._detect_table_patterns()` is preserved as-is.

### `carta/vision/router.py` — rewritten

**Removes:** `DualExtractionRouter`, `_extract_hybrid()`, `ContentType`-based routing.

**Adds:** `SmartRouter` with four route methods:

- `_route_pure_text()` → returns `[]` immediately
- `_route_structured(page, page_num)` → GLM-OCR on full page render
- `_route_text_with_images(page, page_num, profile)` → LLaVA per image crop (pipeline already owns the PyMuPDF text chunk; this method adds only image description chunks); falls back to full page render if no `get_images()` results (vector graphic case)
- `_route_flattened(page, page_num)` → GLM-OCR; if yield < `FLATTENED_MIN_YIELD` → LLaVA full page

**New helper:**

```python
def _extract_image_crops(self, page: Any) -> list[tuple[int, bytes]]:
    """Returns (image_index, png_bytes) for each embedded image,
    sorted by bounding box area descending, capped at MAX_IMAGES_PER_PAGE."""
```

Uses `fitz.Pixmap(doc, xref)` per image xref rather than rendering the full page.

**Unchanged:** `extract_image_descriptions_intelligent(pdf_path, cfg, progress_callback)` — same signature, same return format. `pipeline.py` requires no changes.

### `carta/embed/pipeline.py` — no changes

Public call site unchanged. The 300s file timeout stays.

## Config

New keys under `embed:` in `.carta/config.yaml` (all optional with defaults):

```yaml
embed:
  vision_text_min_chars: 150       # below → FLATTENED
  vision_text_max_chars: 600       # above → captions treated as cross-refs
  vision_flattened_min_yield: 50   # GLM-OCR char yield below this → LLaVA fallback
  vision_max_images_per_page: 4    # cap LLaVA calls per page
```

Existing keys (`ocr_model`, `ollama_vision_model`, `ollama_url`) unchanged. The `vision_routing` key is ignored/deprecated.

## Chunk Output Format

`doc_type: "image_description"` preserved on all chunks for Qdrant backward compatibility. New additive field `page_class` available for future filtering.


| Page class         | Chunks returned  | `model_used`             | `image_index` |
| ------------------ | ---------------- | ------------------------ | ------------- |
| `PURE_TEXT`        | 0                | —                        | —             |
| `STRUCTURED_TEXT`  | 1 (full page)    | `"glm-ocr"`              | 0             |
| `TEXT_WITH_IMAGES` | 1 per image crop | `"llava"`                | 0, 1, 2…      |
| `FLATTENED`        | 1 (full page)    | `"glm-ocr"` or `"llava"` | 0             |


For `TEXT_WITH_IMAGES`, the PyMuPDF text chunk is created by the existing pipeline — vision returns only the image description chunks.

## Testing

- Unit tests for `PageAnalyzer.analyze()` covering all four page classes using mock PyMuPDF pages
- Unit test for figure caption regex: confirm fires on "Fig. 3", "see diagram 12", misses "configured" and "figment"
- Unit test for `TEXT_MAX` gate: caption + dense text → `PURE_TEXT` (not `TEXT_WITH_IMAGES`)
- Unit test for `_extract_image_crops()`: respects area sort and `MAX_IMAGES_PER_PAGE` cap
- Integration test: mock Ollama, embed a multi-page PDF, assert PURE_TEXT pages produce zero Ollama calls
- Regression: existing sidecar `.embed-meta.yaml` format unaffected

