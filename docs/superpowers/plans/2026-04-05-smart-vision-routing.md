# Smart Vision Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken `ContentClassifier`/`DualExtractionRouter` vision pipeline with `PageAnalyzer`/`SmartRouter` so that PURE_TEXT pages cost zero Ollama calls and datasheet embeds complete within the 300s file timeout.

**Architecture:** `PageAnalyzer` reads PyMuPDF metadata (zero model calls) and returns a `PageProfile` with a `PageClass` enum. `SmartRouter` dispatches each page to one of four route methods based on that class. The public entry point `extract_image_descriptions_intelligent` is unchanged so `pipeline.py` requires no edits.

**Tech Stack:** Python 3.10+, PyMuPDF (fitz), requests, unittest.mock, pytest

**Spec:** `docs/superpowers/specs/2026-04-05-smart-vision-routing-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `carta/vision/classifier.py` | **Rewrite** | `PageClass` enum, `PageProfile` dataclass, `PageAnalyzer` |
| `carta/vision/router.py` | **Rewrite** | `SmartRouter`, `extract_image_descriptions_intelligent` |
| `carta/config.py` | **Modify** | Add 4 new vision threshold defaults under `embed:` |
| `carta/vision/tests/test_classifier.py` | **Replace** | Tests for `PageAnalyzer` |
| `carta/vision/tests/test_router.py` | **Replace** | Tests for `SmartRouter` |
| `carta/vision/tests/test_integration.py` | **Replace** | Integration: pure-text PDF produces zero model calls |
| `carta/vision/chunking.py` | **Untouched** | No changes needed |
| `carta/vision/tests/test_chunking.py` | **Untouched** | No changes needed |
| `carta/embed/pipeline.py` | **Untouched** | Calls `extract_image_descriptions_intelligent` — unchanged |

---

## Task 1: Add config defaults for vision thresholds

**Files:**
- Modify: `carta/config.py`
- Test: `carta/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `carta/tests/test_config.py`:

```python
class TestVisionThresholdDefaults:
    def test_vision_text_min_chars_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_text_min_chars"] == 150

    def test_vision_text_max_chars_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_text_max_chars"] == 600

    def test_vision_flattened_min_yield_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_flattened_min_yield"] == 50

    def test_vision_max_images_per_page_default(self):
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["vision_max_images_per_page"] == 4
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest carta/tests/test_config.py::TestVisionThresholdDefaults -v
```

Expected: FAIL — `KeyError: 'vision_text_min_chars'`

- [ ] **Step 3: Add defaults to config.py**

In `carta/config.py`, find the `"embed"` dict. After the `"vision_routing": "auto"` line, add:

```python
        "vision_text_min_chars": 150,      # below → FLATTENED
        "vision_text_max_chars": 600,      # above → captions are cross-refs, skip
        "vision_flattened_min_yield": 50,  # GLM-OCR chars below this → LLaVA fallback
        "vision_max_images_per_page": 4,   # cap LLaVA calls per page (largest first)
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest carta/tests/test_config.py::TestVisionThresholdDefaults -v
```

Expected: 4 passed

- [ ] **Step 5: Confirm existing config tests still pass**

```
pytest carta/tests/test_config.py -v
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add carta/config.py carta/tests/test_config.py
git commit -m "feat: add vision threshold defaults to embed config"
```

---

## Task 2: Rewrite classifier.py with PageAnalyzer

**Files:**
- Rewrite: `carta/vision/classifier.py`
- Replace: `carta/vision/tests/test_classifier.py`

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `carta/vision/tests/test_classifier.py` with:

```python
"""Tests for carta.vision.classifier — PageAnalyzer and PageClass."""
import pytest
from unittest.mock import MagicMock

from carta.vision.classifier import PageAnalyzer, PageClass, PageProfile, FIGURE_CAPTION_RE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_page(text: str = "", images: list = None, blocks: list = None) -> MagicMock:
    """Build a minimal mock fitz Page for PageAnalyzer.analyze()."""
    page = MagicMock()

    def _get_text(fmt: str = "text", **kw):
        if fmt == "blocks":
            return blocks or []
        return text

    page.get_text.side_effect = _get_text
    page.get_images.return_value = images or []
    return page


def _table_blocks() -> list:
    """6 blocks in 2 columns (x=10, x=200) — triggers column-alignment table detection."""
    return [
        (10,  0, 100, 10, "a", 0, 0),
        (10, 20, 100, 30, "b", 0, 0),
        (10, 40, 100, 50, "c", 0, 0),
        (200,  0, 300, 10, "d", 0, 0),
        (200, 20, 300, 30, "e", 0, 0),
        (200, 40, 300, 50, "f", 0, 0),
    ]


# ---------------------------------------------------------------------------
# PageClass assignment
# ---------------------------------------------------------------------------

class TestPageClassPureText:
    def test_long_text_no_images_no_tables(self):
        """200 chars, no images, no tables, no captions → PURE_TEXT."""
        analyzer = PageAnalyzer({})
        page = _make_page(text="x" * 200)
        assert analyzer.analyze(page).page_class == PageClass.PURE_TEXT

    def test_profile_fields_populated(self):
        analyzer = PageAnalyzer({})
        page = _make_page(text="x" * 200)
        profile = analyzer.analyze(page)
        assert profile.text_length == 200
        assert not profile.has_images
        assert not profile.has_tables
        assert not profile.has_captions


class TestPageClassFlattened:
    def test_short_text_is_flattened(self):
        """4 chars < 150 → FLATTENED."""
        analyzer = PageAnalyzer({})
        assert analyzer.analyze(_make_page(text="tiny")).page_class == PageClass.FLATTENED

    def test_empty_page_is_flattened(self):
        analyzer = PageAnalyzer({})
        assert analyzer.analyze(_make_page(text="")).page_class == PageClass.FLATTENED

    def test_custom_text_min_respected(self):
        """vision_text_min_chars=50: 60-char page → PURE_TEXT."""
        analyzer = PageAnalyzer({"embed": {"vision_text_min_chars": 50}})
        assert analyzer.analyze(_make_page(text="x" * 60)).page_class == PageClass.PURE_TEXT


class TestPageClassStructuredText:
    def test_table_blocks_route_to_structured(self):
        analyzer = PageAnalyzer({})
        page = _make_page(text="x" * 200, blocks=_table_blocks())
        profile = analyzer.analyze(page)
        assert profile.page_class == PageClass.STRUCTURED_TEXT
        assert profile.has_tables

    def test_tables_take_priority_over_images(self):
        """STRUCTURED_TEXT wins when both table and image signals present."""
        analyzer = PageAnalyzer({})
        page = _make_page(
            text="x" * 200,
            images=[(1, 0, 100, 100, 8, 0, 0)],
            blocks=_table_blocks(),
        )
        assert analyzer.analyze(page).page_class == PageClass.STRUCTURED_TEXT


class TestPageClassTextWithImages:
    def test_embedded_image_triggers(self):
        """text ≥ MIN + embedded image → TEXT_WITH_IMAGES."""
        analyzer = PageAnalyzer({})
        page = _make_page(text="x" * 200, images=[(1, 0, 100, 100, 8, 0, 0)])
        profile = analyzer.analyze(page)
        assert profile.page_class == PageClass.TEXT_WITH_IMAGES
        assert profile.has_images

    def test_caption_below_text_max_triggers(self):
        """Caption + 300 chars (< 600 MAX) + no images → TEXT_WITH_IMAGES."""
        analyzer = PageAnalyzer({})
        text = "See Figure 3 for the timing diagram. " + "x" * 263
        page = _make_page(text=text)
        profile = analyzer.analyze(page)
        assert profile.has_captions
        assert profile.page_class == PageClass.TEXT_WITH_IMAGES

    def test_caption_above_text_max_ignored(self):
        """Caption + text > 600 → PURE_TEXT (cross-reference to another page)."""
        analyzer = PageAnalyzer({})
        text = "See Figure 12 for details. " + "x" * 600
        page = _make_page(text=text)
        profile = analyzer.analyze(page)
        assert profile.has_captions
        assert profile.page_class == PageClass.PURE_TEXT

    def test_custom_text_max_respected(self):
        """vision_text_max_chars=300 config: 350-char + caption → PURE_TEXT."""
        analyzer = PageAnalyzer({"embed": {"vision_text_max_chars": 300}})
        text = "See Figure 5. " + "x" * 336
        assert analyzer.analyze(_make_page(text=text)).page_class == PageClass.PURE_TEXT


# ---------------------------------------------------------------------------
# Figure caption regex
# ---------------------------------------------------------------------------

class TestFigureCaptionRegex:
    @pytest.mark.parametrize("text", [
        "Fig. 3", "Figure 12", "see plot 4", "Chart 1 shows voltage",
        "diagram 7", "graph 2", "FIG. 1", "FIGURE 3",
    ])
    def test_matches(self, text):
        assert FIGURE_CAPTION_RE.search(text), f"Expected match for: {text!r}"

    @pytest.mark.parametrize("text", [
        "configured the system", "figment of imagination", "figure of speech",
        "reconfigured", "charted course",
    ])
    def test_no_false_positives(self, text):
        assert not FIGURE_CAPTION_RE.search(text), f"Unexpected match for: {text!r}"


# ---------------------------------------------------------------------------
# Table detection
# ---------------------------------------------------------------------------

class TestTableDetection:
    def test_two_columns_detected(self):
        analyzer = PageAnalyzer({})
        page = _make_page(text="x" * 200, blocks=_table_blocks())
        assert analyzer.analyze(page).has_tables

    def test_single_column_not_a_table(self):
        """All blocks at same x → no table."""
        analyzer = PageAnalyzer({})
        blocks = [(50, i * 20, 500, i * 20 + 15, f"line {i}", 0, 0) for i in range(6)]
        page = _make_page(text="x" * 200, blocks=blocks)
        assert not analyzer.analyze(page).has_tables

    def test_too_few_blocks_not_a_table(self):
        """< 4 blocks → table detection skipped."""
        analyzer = PageAnalyzer({})
        blocks = [(10, 0, 100, 10, "a", 0, 0), (200, 0, 300, 10, "b", 0, 0)]
        page = _make_page(text="x" * 200, blocks=blocks)
        assert not analyzer.analyze(page).has_tables
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest carta/vision/tests/test_classifier.py -v
```

Expected: FAIL — `ImportError: cannot import name 'PageAnalyzer'`

- [ ] **Step 3: Rewrite carta/vision/classifier.py**

Replace the entire file contents:

```python
"""PDF page classification for smart vision routing.

Analyzes PyMuPDF page objects using free metadata (zero model calls) to
determine the appropriate extraction strategy for each page.
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


FIGURE_CAPTION_RE = re.compile(
    r'\b(fig\.?|figure|plot|chart|diagram|graph)\s*\d+\b',
    re.IGNORECASE,
)


class PageClass(Enum):
    """Routing class for a PDF page."""
    PURE_TEXT = "pure_text"
    STRUCTURED_TEXT = "structured_text"
    TEXT_WITH_IMAGES = "text_with_images"
    FLATTENED = "flattened"


@dataclass
class PageProfile:
    """Signals extracted from a PDF page for routing decisions.

    Attributes:
        text_length:  Characters returned by page.get_text()
        has_images:   True if page.get_images() is non-empty
        has_tables:   True if column-alignment heuristic fires
        has_captions: True if FIGURE_CAPTION_RE matches page text
        page_class:   Resulting routing class
    """
    text_length: int
    has_images: bool
    has_tables: bool
    has_captions: bool
    page_class: PageClass


class PageAnalyzer:
    """Classify a PDF page using only PyMuPDF metadata (zero model calls).

    Args:
        cfg: Carta config dict. Reads embed.vision_text_min_chars and
             embed.vision_text_max_chars.
    """

    def __init__(self, cfg: dict):
        embed = cfg.get("embed", {})
        self.text_min: int = embed.get("vision_text_min_chars", 150)
        self.text_max: int = embed.get("vision_text_max_chars", 600)

    def analyze(self, page: Any) -> PageProfile:
        """Return a PageProfile for *page*.

        Args:
            page: PyMuPDF fitz.Page object.

        Returns:
            PageProfile with page_class set.
        """
        text = page.get_text().strip()
        text_length = len(text)
        has_images = bool(page.get_images())
        has_tables = self._detect_tables(page)
        has_captions = bool(FIGURE_CAPTION_RE.search(text))
        page_class = self._classify(text_length, has_images, has_tables, has_captions)
        return PageProfile(
            text_length=text_length,
            has_images=has_images,
            has_tables=has_tables,
            has_captions=has_captions,
            page_class=page_class,
        )

    def _classify(
        self,
        text_length: int,
        has_images: bool,
        has_tables: bool,
        has_captions: bool,
    ) -> PageClass:
        if text_length < self.text_min:
            return PageClass.FLATTENED
        if has_tables:
            return PageClass.STRUCTURED_TEXT
        if has_images or (has_captions and text_length < self.text_max):
            return PageClass.TEXT_WITH_IMAGES
        return PageClass.PURE_TEXT

    def _detect_tables(self, page: Any) -> bool:
        """Detect table structures via column-alignment heuristic."""
        try:
            blocks = page.get_text("blocks")
            if not blocks or len(blocks) < 4:
                return False
            x_positions = [
                b[0] for b in blocks
                if isinstance(b, (tuple, list)) and len(b) >= 1
                and isinstance(b[0], (int, float))
            ]
            if len(x_positions) < 4:
                return False
            return self._has_column_alignment(x_positions)
        except Exception:
            return False

    def _has_column_alignment(self, x_positions: list[float]) -> bool:
        """Return True if x-positions cluster into ≥2 columns with ≥2 blocks each."""
        tolerance = 20
        clusters: list[list[float]] = []
        for x in sorted(x_positions):
            for cluster in clusters:
                if abs(x - cluster[0]) < tolerance:
                    cluster.append(x)
                    break
            else:
                clusters.append([x])
        return len([c for c in clusters if len(c) >= 2]) >= 2
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest carta/vision/tests/test_classifier.py -v
```

Expected: all pass

- [ ] **Step 5: Confirm no regressions in the rest of the suite**

```
pytest carta/tests/ -q
```

Expected: all pass (the old `test_vision.py` tests `carta.embed.vision` which is untouched)

- [ ] **Step 6: Commit**

```bash
git add carta/vision/classifier.py carta/vision/tests/test_classifier.py
git commit -m "feat: replace ContentClassifier with PageAnalyzer (zero model calls for routing)"
```

---

## Task 3: Rewrite router.py with SmartRouter

**Files:**
- Rewrite: `carta/vision/router.py`
- Replace: `carta/vision/tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `carta/vision/tests/test_router.py` with:

```python
"""Tests for carta.vision.router — SmartRouter and extract_image_descriptions_intelligent."""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from carta.vision.router import SmartRouter, extract_image_descriptions_intelligent
from carta.vision.classifier import PageClass, PageProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**embed_overrides) -> dict:
    base = {"ollama_url": "http://localhost:11434"}
    base.update(embed_overrides)
    return {"embed": base}


def _profile(page_class: PageClass, **kw) -> PageProfile:
    defaults = dict(text_length=300, has_images=False, has_tables=False, has_captions=False)
    defaults.update(kw)
    return PageProfile(**defaults, page_class=page_class)


def _pixmap(content: bytes = b"fakepng") -> MagicMock:
    pix = MagicMock()
    pix.tobytes.return_value = content
    return pix


# ---------------------------------------------------------------------------
# _route dispatch
# ---------------------------------------------------------------------------

class TestRoutePureText:
    def test_returns_empty_list_no_model_calls(self):
        """PURE_TEXT → [] with zero model calls."""
        router = SmartRouter(_cfg())
        page = MagicMock()
        with patch.object(router, "_call_ollama_vision") as mock_call:
            result = router._route(page, 1, _profile(PageClass.PURE_TEXT), MagicMock())
        assert result == []
        mock_call.assert_not_called()


class TestRouteStructured:
    def test_calls_glm_ocr_once(self):
        """STRUCTURED_TEXT → 1 GLM-OCR call, 1 chunk."""
        router = SmartRouter(_cfg(ocr_model="glm-ocr:latest"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", return_value="OCR text") as mock_call:
            result = router._route(page, 2, _profile(PageClass.STRUCTURED_TEXT), MagicMock())
        mock_call.assert_called_once()
        assert mock_call.call_args[1]["model"] == "glm-ocr:latest"
        assert len(result) == 1
        assert result[0]["model_used"] == "glm-ocr"
        assert result[0]["page_class"] == "structured_text"
        assert result[0]["page_num"] == 2

    def test_glm_failure_returns_empty(self):
        """GLM-OCR exception → [] (fail-open)."""
        router = SmartRouter(_cfg())
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", side_effect=RuntimeError("timeout")):
            result = router._route(page, 1, _profile(PageClass.STRUCTURED_TEXT), MagicMock())
        assert result == []


class TestRouteTextWithImages:
    def test_each_crop_gets_llava_call(self):
        """2 image crops → 2 LLaVA calls, 2 chunks with correct image_index."""
        router = SmartRouter(_cfg(ollama_vision_model="llava:latest"))
        page = MagicMock()
        with patch.object(router, "_extract_image_crops", return_value=[(0, b"img0"), (1, b"img1")]):
            with patch.object(router, "_call_ollama_vision", return_value="desc") as mock_call:
                result = router._route(
                    page, 3, _profile(PageClass.TEXT_WITH_IMAGES, has_images=True), MagicMock()
                )
        assert mock_call.call_count == 2
        assert all(c["model_used"] == "llava" for c in result)
        assert [c["image_index"] for c in result] == [0, 1]

    def test_no_crops_falls_back_to_full_page_render(self):
        """No get_images() results (vector graphic) → full-page render + LLaVA."""
        router = SmartRouter(_cfg())
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_extract_image_crops", return_value=[]):
            with patch.object(router, "_call_ollama_vision", return_value="vector desc") as mock_call:
                result = router._route(
                    page, 1, _profile(PageClass.TEXT_WITH_IMAGES, has_captions=True), MagicMock()
                )
        page.get_pixmap.assert_called_once()
        assert len(result) == 1
        assert result[0]["text"] == "vector desc"

    def test_llava_failure_per_crop_is_skipped(self):
        """LLaVA failure on one crop is skipped; others still processed."""
        router = SmartRouter(_cfg())
        page = MagicMock()
        with patch.object(router, "_extract_image_crops", return_value=[(0, b"img0"), (1, b"img1")]):
            with patch.object(
                router, "_call_ollama_vision",
                side_effect=[RuntimeError("timeout"), "second desc"]
            ):
                result = router._route(
                    page, 1, _profile(PageClass.TEXT_WITH_IMAGES, has_images=True), MagicMock()
                )
        assert len(result) == 1
        assert result[0]["text"] == "second desc"


class TestRouteFlattened:
    def test_high_ocr_yield_returns_glm_ocr_chunk(self):
        """GLM-OCR yield ≥ 50 chars → 1 call, model_used=glm-ocr."""
        router = SmartRouter(_cfg(vision_flattened_min_yield=50))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", return_value="x" * 60) as mock_call:
            result = router._route(page, 1, _profile(PageClass.FLATTENED), MagicMock())
        mock_call.assert_called_once()
        assert result[0]["model_used"] == "glm-ocr"

    def test_low_ocr_yield_falls_back_to_llava(self):
        """GLM-OCR yield < 50 → second call with LLaVA model."""
        router = SmartRouter(_cfg(
            vision_flattened_min_yield=50,
            ocr_model="glm-ocr:latest",
            ollama_vision_model="llava:latest",
        ))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(
            router, "_call_ollama_vision",
            side_effect=["short", "full image description"]
        ) as mock_call:
            result = router._route(page, 1, _profile(PageClass.FLATTENED), MagicMock())
        assert mock_call.call_count == 2
        assert "glm" in mock_call.call_args_list[0][1]["model"]
        assert "llava" in mock_call.call_args_list[1][1]["model"]
        assert result[0]["model_used"] == "llava"

    def test_vision_failure_returns_empty(self):
        """Exception on both calls → [] (fail-open)."""
        router = SmartRouter(_cfg())
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", side_effect=RuntimeError("down")):
            result = router._route(page, 1, _profile(PageClass.FLATTENED), MagicMock())
        assert result == []


# ---------------------------------------------------------------------------
# _extract_image_crops
# ---------------------------------------------------------------------------

class TestExtractImageCrops:
    def test_caps_at_max_images_per_page(self):
        """5 images on page, max=2 → 2 crops returned."""
        router = SmartRouter(_cfg(vision_max_images_per_page=2))
        images = [(i, 0, 100, 100, 8, 0, 0) for i in range(1, 6)]
        page = MagicMock()
        page.get_images.return_value = images
        mock_rect = MagicMock()
        mock_rect.width = 100
        mock_rect.height = 100
        page.get_image_rects.return_value = [mock_rect]
        mock_pix = MagicMock()
        mock_pix.tobytes.return_value = b"fakepng"
        with patch("carta.vision.router.fitz") as mock_fitz:
            mock_fitz.csRGB = "RGB"
            mock_fitz.csGRAY = "GRAY"
            mock_pix.colorspace = "RGB"
            mock_fitz.Pixmap.return_value = mock_pix
            crops = router._extract_image_crops(page, MagicMock())
        assert len(crops) == 2

    def test_no_images_returns_empty(self):
        """Page with no images → []."""
        router = SmartRouter(_cfg())
        page = MagicMock()
        page.get_images.return_value = []
        assert router._extract_image_crops(page, MagicMock()) == []


# ---------------------------------------------------------------------------
# Chunk output format
# ---------------------------------------------------------------------------

class TestChunkOutputFormat:
    def test_required_fields_present(self):
        """All required chunk fields present and correct."""
        router = SmartRouter(_cfg())
        chunk = router._make_chunk(5, 2, "some text", "llava", "text_with_images")
        assert chunk["doc_type"] == "image_description"
        assert chunk["page_num"] == 5
        assert chunk["image_index"] == 2
        assert chunk["text"] == "some text"
        assert chunk["model_used"] == "llava"
        assert chunk["page_class"] == "text_with_images"
        assert chunk["content_type"] == "text_with_images"


# ---------------------------------------------------------------------------
# _call_ollama_vision
# ---------------------------------------------------------------------------

class TestCallOllamaVision:
    def test_returns_stripped_response(self):
        router = SmartRouter(_cfg())
        with patch("carta.vision.router.requests") as mock_requests:
            mock_requests.post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value={"response": "  description  "})
            )
            result = router._call_ollama_vision(b"fakepng", model="llava", prompt="describe")
        assert result == "description"

    def test_raises_on_non_200(self):
        router = SmartRouter(_cfg())
        with patch("carta.vision.router.requests") as mock_requests:
            mock_requests.post.return_value = MagicMock(status_code=503, text="unavailable")
            with pytest.raises(RuntimeError, match="503"):
                router._call_ollama_vision(b"fakepng", model="llava", prompt="describe")


# ---------------------------------------------------------------------------
# extract_image_descriptions_intelligent (public API)
# ---------------------------------------------------------------------------

class TestPublicAPI:
    def test_pure_text_pdf_produces_no_model_calls(self):
        """3-page text-only PDF → [] with zero requests.post calls."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}

        def make_page():
            page = MagicMock()
            page.get_text.side_effect = lambda fmt="text", **kw: (
                [] if fmt == "blocks" else "x" * 300
            )
            page.get_images.return_value = []
            return page

        with patch("carta.vision.router.fitz") as mock_fitz:
            doc = MagicMock()
            doc.__iter__ = MagicMock(return_value=iter([make_page() for _ in range(3)]))
            doc.__len__ = MagicMock(return_value=3)
            mock_fitz.open.return_value = doc
            with patch("carta.vision.router.requests") as mock_requests:
                result = extract_image_descriptions_intelligent(Path("fake.pdf"), cfg)
        assert result == []
        mock_requests.post.assert_not_called()

    def test_returns_list(self):
        """Always returns a list."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        with patch("carta.vision.router.fitz") as mock_fitz:
            doc = MagicMock()
            doc.__iter__ = MagicMock(return_value=iter([]))
            doc.__len__ = MagicMock(return_value=0)
            mock_fitz.open.return_value = doc
            result = extract_image_descriptions_intelligent(Path("fake.pdf"), cfg)
        assert isinstance(result, list)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest carta/vision/tests/test_router.py -v
```

Expected: FAIL — `ImportError: cannot import name 'SmartRouter'`

- [ ] **Step 3: Rewrite carta/vision/router.py**

Replace the entire file contents:

```python
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
            if len(ocr_text) >= self.flattened_min_yield:
                return [self._make_chunk(page_num, 0, ocr_text, "glm-ocr", "flattened")]
            # Low yield — page is likely a photo or decorative image, try LLaVA
            vision_text = self._call_ollama_vision(
                png_bytes, model=self.vision_model, prompt=LLAVA_PROMPT
            )
            return [self._make_chunk(page_num, 0, vision_text, "llava", "flattened")]
        except Exception as exc:
            print(
                f"Warning: vision failed for flattened page {page_num}: {exc}",
                file=sys.stderr, flush=True,
            )
            return []

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
            "content_type": page_class_str,
            "page_class": page_class_str,
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
        model_used, content_type, page_class.
    """
    router = SmartRouter(cfg)
    return router.extract_pdf(pdf_path, progress_callback)
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest carta/vision/tests/test_router.py -v
```

Expected: all pass

- [ ] **Step 5: Verify the pipeline's call site still works**

```
pytest carta/tests/test_pipeline.py -v
```

Expected: all pass (pipeline.py is unchanged and the public API signature is identical)

- [ ] **Step 6: Commit**

```bash
git add carta/vision/router.py carta/vision/tests/test_router.py
git commit -m "feat: replace DualExtractionRouter with SmartRouter (skip PURE_TEXT pages)"
```

---

## Task 4: Replace test_integration.py and run full suite

**Files:**
- Replace: `carta/vision/tests/test_integration.py`

- [ ] **Step 1: Replace test_integration.py**

The old file imports `DualExtractionRouter`, `ContentClassifier`, `ExtractionResult` — all removed. Replace with a minimal integration test that verifies the public API contract:

```python
"""Integration tests for smart vision routing pipeline."""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from carta.vision.router import extract_image_descriptions_intelligent
from carta.vision.classifier import PageClass


class TestSmartRoutingIntegration:
    def test_mixed_page_types_pdf(self):
        """PDF with mixed page types produces correct chunk counts and model_used values."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ocr_model": "glm-ocr:latest",
                "ollama_vision_model": "llava:latest",
                "vision_text_min_chars": 150,
                "vision_text_max_chars": 600,
                "vision_flattened_min_yield": 50,
                "vision_max_images_per_page": 4,
            }
        }

        def make_pure_text_page():
            page = MagicMock()
            page.get_text.side_effect = lambda fmt="text", **kw: (
                [] if fmt == "blocks" else "x" * 300
            )
            page.get_images.return_value = []
            return page

        def make_flattened_page():
            page = MagicMock()
            page.get_text.side_effect = lambda fmt="text", **kw: (
                [] if fmt == "blocks" else ""  # no extractable text
            )
            page.get_images.return_value = []
            pix = MagicMock()
            pix.tobytes.return_value = b"fakepng"
            page.get_pixmap.return_value = pix
            return page

        # 2 pure-text pages, 1 flattened page
        pages = [make_pure_text_page(), make_pure_text_page(), make_flattened_page()]

        with patch("carta.vision.router.fitz") as mock_fitz:
            doc = MagicMock()
            doc.__iter__ = MagicMock(return_value=iter(pages))
            doc.__len__ = MagicMock(return_value=3)
            mock_fitz.open.return_value = doc

            with patch("carta.vision.router.requests") as mock_requests:
                # GLM-OCR returns enough text on flattened page
                mock_requests.post.return_value = MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"response": "x" * 60})
                )
                result = extract_image_descriptions_intelligent(Path("fake.pdf"), cfg)

        # 2 pure-text pages → 0 calls; 1 flattened → 1 GLM-OCR call
        assert mock_requests.post.call_count == 1
        assert len(result) == 1
        assert result[0]["model_used"] == "glm-ocr"
        assert result[0]["page_class"] == "flattened"

    def test_public_api_chunk_fields(self):
        """Returned chunks have all fields expected by pipeline.py."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}

        def make_flattened_page():
            page = MagicMock()
            page.get_text.side_effect = lambda fmt="text", **kw: (
                [] if fmt == "blocks" else ""
            )
            page.get_images.return_value = []
            pix = MagicMock()
            pix.tobytes.return_value = b"fakepng"
            page.get_pixmap.return_value = pix
            return page

        with patch("carta.vision.router.fitz") as mock_fitz:
            doc = MagicMock()
            doc.__iter__ = MagicMock(return_value=iter([make_flattened_page()]))
            doc.__len__ = MagicMock(return_value=1)
            mock_fitz.open.return_value = doc

            with patch("carta.vision.router.requests") as mock_requests:
                mock_requests.post.return_value = MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"response": "x" * 60})
                )
                result = extract_image_descriptions_intelligent(Path("fake.pdf"), cfg)

        assert len(result) == 1
        chunk = result[0]
        for field in ["doc_type", "page_num", "image_index", "text", "model_used", "page_class"]:
            assert field in chunk, f"Missing field: {field}"
        assert chunk["doc_type"] == "image_description"
```

- [ ] **Step 2: Run the new integration tests**

```
pytest carta/vision/tests/test_integration.py -v
```

Expected: all pass

- [ ] **Step 3: Run the full vision test suite**

```
pytest carta/vision/tests/ -v
```

Expected: all pass (test_chunking.py untouched and still passes)

- [ ] **Step 4: Run the complete test suite**

```
pytest carta/ -q
```

Expected: all pass. If any failures appear in `carta/tests/test_vision.py`, check that `carta.embed.vision` was not accidentally modified — it is separate from `carta.vision.router`.

- [ ] **Step 5: Commit**

```bash
git add carta/vision/tests/test_integration.py
git commit -m "test: replace integration tests for smart vision routing"
```

---

## Done

After all tasks pass:

```
pytest carta/ -q
```

Expected output includes: all tests pass, no failures related to `ContentClassifier`, `DualExtractionRouter`, or `ExtractionResult`.

The `carta embed` command will now skip vision model calls for pure-text pages. A 100-page datasheet where most pages are text should complete well under the 300s file timeout.
