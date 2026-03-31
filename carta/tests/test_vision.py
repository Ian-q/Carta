"""Tests for carta.embed.vision — image extraction and Ollama vision pipeline."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers: mock fitz objects
# ---------------------------------------------------------------------------

def _make_pixmap(colorspace, width=100, height=100):
    """Return a mock fitz.Pixmap with given colorspace and dimensions."""
    pix = MagicMock()
    pix.colorspace = colorspace
    pix.width = width
    pix.height = height
    pix.tobytes = MagicMock(return_value=b"fakepng")
    return pix


def _make_page(image_xrefs=None, drawing_count=0, pixmap=None):
    """Return a mock fitz Page.

    image_xrefs: list of xref ints (each becomes a tuple where [0] is xref)
    drawing_count: number of drawing dicts returned by get_drawings()
    """
    page = MagicMock()
    if image_xrefs is None:
        image_xrefs = []
    # get_images returns list of tuples; element [0] is xref
    page.get_images.return_value = [(xref, 0, 0, 0, 0, 0, 0) for xref in image_xrefs]
    page.get_drawings.return_value = [{"type": "re"} for _ in range(drawing_count)]
    if pixmap is None:
        pixmap = MagicMock()
        pixmap.tobytes = MagicMock(return_value=b"renderedpng")
    page.get_pixmap.return_value = pixmap
    return page


def _make_doc(pages, pixmap_factory=None):
    """Return a mock fitz Document wrapping given page mocks."""
    doc = MagicMock()
    doc.__iter__ = MagicMock(return_value=iter(pages))
    doc.__len__ = MagicMock(return_value=len(pages))
    if pixmap_factory is not None:
        doc.extract_image = MagicMock(side_effect=pixmap_factory)
    return doc


# ---------------------------------------------------------------------------
# Test: config default
# ---------------------------------------------------------------------------

class TestConfigVisionModelDefault:
    def test_config_vision_model_default(self):
        """DEFAULTS['embed']['ollama_vision_model'] must be 'llava:latest'."""
        from carta.config import DEFAULTS
        assert DEFAULTS["embed"]["ollama_vision_model"] == "llava:latest"


# ---------------------------------------------------------------------------
# Test: VISION_PROMPT constant
# ---------------------------------------------------------------------------

class TestVisionPromptConstant:
    def test_vision_prompt_constant(self):
        """VISION_PROMPT is a module-level string constant <= 200 chars."""
        from carta.embed.vision import VISION_PROMPT
        assert isinstance(VISION_PROMPT, str)
        assert len(VISION_PROMPT) <= 200, (
            f"VISION_PROMPT is {len(VISION_PROMPT)} chars (must be <= 200)"
        )


# ---------------------------------------------------------------------------
# Test: no images → empty list
# ---------------------------------------------------------------------------

class TestNoImages:
    @patch("carta.embed.vision.fitz")
    def test_no_images_returns_empty(self, mock_fitz):
        """PDF with no images and no significant drawings returns []."""
        from carta.embed.vision import extract_image_descriptions

        page = _make_page(image_xrefs=[], drawing_count=0)
        doc = _make_doc([page])
        mock_fitz.open.return_value = doc
        mock_fitz.csRGB = object()
        mock_fitz.csGRAY = object()

        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_vision_model": "llava:latest",
            }
        }

        result = extract_image_descriptions(Path("fake.pdf"), cfg)
        assert result == []


# ---------------------------------------------------------------------------
# Test: chunk metadata shape
# ---------------------------------------------------------------------------

class TestChunkMetadata:
    @patch("requests.post")
    @patch("carta.embed.vision.fitz")
    def test_chunk_metadata(self, mock_fitz, mock_post):
        """Each returned dict has doc_type, page_num, image_index, text keys."""
        from carta.embed.vision import extract_image_descriptions

        cs_rgb = object()
        pix = _make_pixmap(colorspace=cs_rgb, width=100, height=100)
        mock_fitz.csRGB = cs_rgb
        mock_fitz.csGRAY = object()
        mock_fitz.Pixmap.return_value = pix

        page = _make_page(image_xrefs=[42])
        doc = _make_doc([page])
        mock_fitz.open.return_value = doc

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"response": "A bar chart showing temperature vs time."})
        )

        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_vision_model": "llava:latest",
            }
        }

        result = extract_image_descriptions(Path("fake.pdf"), cfg)

        assert len(result) == 1
        item = result[0]
        assert item["doc_type"] == "image_description"
        assert "page_num" in item
        assert "image_index" in item
        assert "text" in item
        assert item["text"] == "A bar chart showing temperature vs time."


# ---------------------------------------------------------------------------
# Test: CMYK conversion
# ---------------------------------------------------------------------------

class TestCmykConversion:
    @patch("requests.post")
    @patch("carta.embed.vision.fitz")
    def test_cmyk_conversion(self, mock_fitz, mock_post):
        """CMYK pixmap is converted to RGB before PNG encoding."""
        from carta.embed.vision import extract_image_descriptions

        cs_rgb = object()
        cs_gray = object()
        cs_cmyk = object()  # not RGB or GRAY → triggers conversion

        mock_fitz.csRGB = cs_rgb
        mock_fitz.csGRAY = cs_gray

        cmyk_pix = _make_pixmap(colorspace=cs_cmyk, width=100, height=100)
        rgb_pix = _make_pixmap(colorspace=cs_rgb, width=100, height=100)

        # First Pixmap() call (xref extraction) returns CMYK pix
        # Second Pixmap() call (conversion) returns RGB pix
        mock_fitz.Pixmap.side_effect = [cmyk_pix, rgb_pix]

        page = _make_page(image_xrefs=[7])
        doc = _make_doc([page])
        mock_fitz.open.return_value = doc

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"response": "description"})
        )

        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_vision_model": "llava:latest",
            }
        }

        extract_image_descriptions(Path("fake.pdf"), cfg)

        # Second Pixmap call should pass csRGB as first arg (conversion)
        calls = mock_fitz.Pixmap.call_args_list
        assert len(calls) == 2
        # The conversion call: fitz.Pixmap(fitz.csRGB, cmyk_pix)
        conversion_call_args = calls[1][0]
        assert conversion_call_args[0] is cs_rgb


# ---------------------------------------------------------------------------
# Test: fail-open on Ollama error
# ---------------------------------------------------------------------------

class TestVisionFailOpen:
    @patch("requests.post")
    @patch("carta.embed.vision.fitz")
    def test_vision_fail_open(self, mock_fitz, mock_post):
        """Ollama error for one image → partial results, no exception raised."""
        from carta.embed.vision import extract_image_descriptions

        cs_rgb = object()
        mock_fitz.csRGB = cs_rgb
        mock_fitz.csGRAY = object()

        pix1 = _make_pixmap(colorspace=cs_rgb)
        pix2 = _make_pixmap(colorspace=cs_rgb)
        mock_fitz.Pixmap.side_effect = [pix1, pix2]

        page = _make_page(image_xrefs=[1, 2])
        doc = _make_doc([page])
        mock_fitz.open.return_value = doc

        # First call fails, second succeeds
        mock_post.side_effect = [
            Exception("connection refused"),
            MagicMock(
                status_code=200,
                json=MagicMock(return_value={"response": "second image description"})
            ),
        ]

        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_vision_model": "llava:latest",
            }
        }

        # Must not raise
        result = extract_image_descriptions(Path("fake.pdf"), cfg)

        # Second image still processed
        assert len(result) == 1
        assert result[0]["text"] == "second image description"


# ---------------------------------------------------------------------------
# Test: page-render fallback (vector drawings, no embedded images)
# ---------------------------------------------------------------------------

class TestPageRenderFallback:
    @patch("requests.post")
    @patch("carta.embed.vision.fitz")
    def test_page_render_fallback(self, mock_fitz, mock_post):
        """Page with 0 images but >=3 drawings triggers get_pixmap(dpi=150)."""
        from carta.embed.vision import extract_image_descriptions

        mock_fitz.csRGB = object()
        mock_fitz.csGRAY = object()

        render_pix = MagicMock()
        render_pix.tobytes = MagicMock(return_value=b"renderedpng")

        page = _make_page(image_xrefs=[], drawing_count=3, pixmap=render_pix)
        doc = _make_doc([page])
        mock_fitz.open.return_value = doc

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"response": "vector diagram description"})
        )

        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_vision_model": "llava:latest",
            }
        }

        result = extract_image_descriptions(Path("fake.pdf"), cfg)

        # get_pixmap must have been called with dpi=150
        page.get_pixmap.assert_called_once_with(dpi=150)
        assert len(result) == 1
        assert result[0]["text"] == "vector diagram description"


# ---------------------------------------------------------------------------
# Test: no fallback for trivial drawings (< 3)
# ---------------------------------------------------------------------------

class TestNoFallbackTrivialDrawings:
    @patch("carta.embed.vision.fitz")
    def test_no_fallback_trivial_drawings(self, mock_fitz):
        """Page with 0 images and <3 drawings is skipped (no get_pixmap call)."""
        from carta.embed.vision import extract_image_descriptions

        mock_fitz.csRGB = object()
        mock_fitz.csGRAY = object()

        page = _make_page(image_xrefs=[], drawing_count=2)
        doc = _make_doc([page])
        mock_fitz.open.return_value = doc

        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_vision_model": "llava:latest",
            }
        }

        result = extract_image_descriptions(Path("fake.pdf"), cfg)

        page.get_pixmap.assert_not_called()
        assert result == []


# ---------------------------------------------------------------------------
# Test: dimension cap (scale down images > 2048px)
# ---------------------------------------------------------------------------

class TestDimensionCap:
    @patch("requests.post")
    @patch("carta.embed.vision.fitz")
    def test_dimension_cap(self, mock_fitz, mock_post):
        """Image with width > 2048 triggers scale-down (get_pixmap called with matrix)."""
        from carta.embed.vision import extract_image_descriptions

        cs_rgb = object()
        mock_fitz.csRGB = cs_rgb
        mock_fitz.csGRAY = object()

        # Large pixmap from xref extraction
        large_pix = _make_pixmap(colorspace=cs_rgb, width=4096, height=2048)
        mock_fitz.Pixmap.return_value = large_pix

        scaled_pix = MagicMock()
        scaled_pix.tobytes = MagicMock(return_value=b"scaledpng")

        page = _make_page(image_xrefs=[99])
        page.get_pixmap.return_value = scaled_pix
        # get_image_rects returns a list with one rect
        page.get_image_rects.return_value = [MagicMock()]

        doc = _make_doc([page])
        mock_fitz.open.return_value = doc
        mock_fitz.Matrix.return_value = MagicMock()

        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"response": "scaled image description"})
        )

        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_vision_model": "llava:latest",
            }
        }

        result = extract_image_descriptions(Path("fake.pdf"), cfg)

        # fitz.Matrix should have been called for scaling
        mock_fitz.Matrix.assert_called()
        assert len(result) == 1
