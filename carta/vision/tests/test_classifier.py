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
