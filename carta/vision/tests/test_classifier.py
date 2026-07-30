"""Tests for carta.vision.classifier — PageAnalyzer and PageClass."""
import pytest
from unittest.mock import MagicMock

from carta.vision.classifier import PageAnalyzer, PageClass, PageProfile, FIGURE_CAPTION_RE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_rect(x0: float, y0: float, x1: float, y1: float):
    """Return a minimal rect-like object with width and height."""
    r = MagicMock()
    r.width = x1 - x0
    r.height = y1 - y0
    return r


def _make_page(
    text: str = "",
    images: list = None,
    blocks: list = None,
    page_rect=None,
    image_rects: dict = None,
    drawings: list = None,
) -> MagicMock:
    """Build a minimal mock fitz Page for PageAnalyzer.analyze().

    Args:
        text: text returned by page.get_text().
        images: list of image tuples (first element is xref int).
        blocks: list of block tuples for "blocks" fmt.
        page_rect: mock rect with .width/.height; defaults to A4 (595×842).
        image_rects: dict mapping xref → rect-like object for get_image_rects().
                     Missing xrefs return []. Provide large rects for "real" images,
                     small rects for logos/decorative images.
        drawings: list returned by page.get_drawings() (vector path count source).
                  Defaults to [] (unconfigured MagicMock().get_drawings() also
                  defaults to len()==0, but this makes intent explicit).
    """
    page = MagicMock()
    page.get_drawings.return_value = drawings or []

    def _get_text(fmt: str = "text", **kw):
        if fmt == "blocks":
            return blocks or []
        return text

    page.get_text.side_effect = _get_text
    page.get_images.return_value = images or []

    # Default A4 page rect (595 × 842 points)
    if page_rect is None:
        page_rect = _mock_rect(0, 0, 595, 842)
    page.rect = page_rect

    # Image rect lookup
    _image_rects = image_rects or {}

    def _get_image_rects(xref):
        return [_image_rects[xref]] if xref in _image_rects else []

    page.get_image_rects.side_effect = _get_image_rects

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
        """STRUCTURED_TEXT wins when both table and significant image signals present."""
        analyzer = PageAnalyzer({})
        page = _make_page(
            text="x" * 200,
            images=[(1, 0, 100, 100, 8, 0, 0)],
            blocks=_table_blocks(),
            image_rects={1: _mock_rect(0, 0, 200, 200)},
        )
        assert analyzer.analyze(page).page_class == PageClass.STRUCTURED_TEXT


class TestPageClassTextWithImages:
    def test_embedded_image_triggers(self):
        """text ≥ MIN + significant embedded image → TEXT_WITH_IMAGES."""
        analyzer = PageAnalyzer({})
        # Image covers 200×200 pts = 40,000 sq pts; 5% of A4 = 25,065 sq pts → significant
        page = _make_page(
            text="x" * 200,
            images=[(1, 0, 100, 100, 8, 0, 0)],
            image_rects={1: _mock_rect(0, 0, 200, 200)},
        )
        profile = analyzer.analyze(page)
        assert profile.page_class == PageClass.TEXT_WITH_IMAGES
        assert profile.has_images

    def test_tiny_logo_with_long_text_is_pure_text(self):
        """Company logo (tiny image) + long text → PURE_TEXT, not TEXT_WITH_IMAGES."""
        analyzer = PageAnalyzer({})
        # Logo covers 72×36 pts = 2,592 sq pts; well below 5% threshold of ~25,065
        page = _make_page(
            text="x" * 200,
            images=[(1, 0, 100, 100, 8, 0, 0)],
            image_rects={1: _mock_rect(0, 0, 72, 36)},
        )
        profile = analyzer.analyze(page)
        assert not profile.has_images
        assert profile.page_class == PageClass.PURE_TEXT

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

class TestPageClassVectorDrawing:
    """Vector-CAD signature: raster-free + drawing-dense + sparse text.

    Closes two misclassification paths: (1) a classifier that would otherwise
    call a dense-drawing, text-light page PURE_TEXT/FLATTENED because it has
    no raster images, and (2) a title block that pushes text_length past 150
    (the FLATTENED threshold) — the vector-CAD check runs FIRST, before the
    text_min check, so it still wins.
    """

    def test_classify_direct_vector_drawing(self):
        """Brief Step 1: raster-free, drawing-dense, sparse text -> VECTOR_DRAWING
        even when text_length exceeds the FLATTENED threshold (title-block case)."""
        analyzer = PageAnalyzer({"embed": {"deep_scan": {"vector_min_paths": 50,
                                                          "vector_text_max_chars": 1000}}})
        assert analyzer._classify(300, False, False, False, drawing_count=200) is PageClass.VECTOR_DRAWING

    def test_classify_raster_images_present_not_vector_cad(self):
        analyzer = PageAnalyzer({"embed": {"deep_scan": {"vector_min_paths": 50,
                                                          "vector_text_max_chars": 1000}}})
        assert analyzer._classify(300, True, False, False, drawing_count=200) is not PageClass.VECTOR_DRAWING

    def test_classify_text_heavy_page_stays_pure_text(self):
        """Text-heavy pages with incidental rules/underlines (many drawings) stay text."""
        analyzer = PageAnalyzer({"embed": {"deep_scan": {"vector_min_paths": 50,
                                                          "vector_text_max_chars": 1000}}})
        assert analyzer._classify(2000, False, False, False, drawing_count=200) is PageClass.PURE_TEXT

    def test_classify_default_thresholds_without_config(self):
        """Defaults (vector_min_paths=50, vector_text_max_chars=1000) apply with bare {} cfg."""
        analyzer = PageAnalyzer({})
        assert analyzer._classify(300, False, False, False, drawing_count=50) is PageClass.VECTOR_DRAWING
        assert analyzer._classify(300, False, False, False, drawing_count=49) is not PageClass.VECTOR_DRAWING

    def test_analyze_computes_drawing_count_from_page(self):
        """analyze() reads len(page.get_drawings()) into profile.drawing_count."""
        analyzer = PageAnalyzer({})
        page = _make_page(text="x" * 300, drawings=[object()] * 75)
        profile = analyzer.analyze(page)
        assert profile.drawing_count == 75
        assert profile.page_class == PageClass.VECTOR_DRAWING

    def test_analyze_falls_back_to_zero_without_get_drawings(self):
        """Pages from a fitz build without get_drawings() must not raise —
        drawing_count falls back to 0 via the hasattr guard."""
        analyzer = PageAnalyzer({})

        class NoDrawingsPage:
            def get_text(self, fmt="text", **kw):
                return [] if fmt == "blocks" else "x" * 300
            def get_images(self):
                return []
            rect = _mock_rect(0, 0, 595, 842)

        profile = analyzer.analyze(NoDrawingsPage())
        assert profile.drawing_count == 0
        assert profile.page_class == PageClass.PURE_TEXT

    def test_vector_drawing_beats_flattened_short_text(self):
        """Even short text (would be FLATTENED) yields VECTOR_DRAWING when the
        vector-CAD signature is present — the check runs before the text_min gate."""
        analyzer = PageAnalyzer({})
        page = _make_page(text="A0", drawings=[object()] * 200)
        assert analyzer.analyze(page).page_class == PageClass.VECTOR_DRAWING

    def test_default_drawing_count_is_zero_in_classify(self):
        """_classify(..., ) without drawing_count must default to 0 (never VECTOR_DRAWING)."""
        analyzer = PageAnalyzer({})
        assert analyzer._classify(300, False, False, False) is not PageClass.VECTOR_DRAWING


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
