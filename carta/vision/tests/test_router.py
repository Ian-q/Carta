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

    def test_llava_fallback_failure_returns_low_yield_ocr(self):
        """LLaVA fallback fails after low-yield OCR → return OCR chunk anyway."""
        router = SmartRouter(_cfg(
            vision_flattened_min_yield=50,
            ocr_model="glm-ocr:latest",
            ollama_vision_model="llava:latest",
        ))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(
            router, "_call_ollama_vision",
            side_effect=["short ocr", RuntimeError("llava down")]
        ):
            result = router._route(page, 1, _profile(PageClass.FLATTENED), MagicMock())
        assert len(result) == 1
        assert result[0]["model_used"] == "glm-ocr"
        assert result[0]["text"] == "short ocr"


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
        cfg = _cfg()

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
        cfg = _cfg()
        with patch("carta.vision.router.fitz") as mock_fitz:
            doc = MagicMock()
            doc.__iter__ = MagicMock(return_value=iter([]))
            doc.__len__ = MagicMock(return_value=0)
            mock_fitz.open.return_value = doc
            result = extract_image_descriptions_intelligent(Path("fake.pdf"), cfg)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# progress_callback — 5-arg post-routing
# ---------------------------------------------------------------------------

class TestExtractPdfProgressCallback:
    """Verify extract_pdf fires callback AFTER routing with 5-arg signature."""

    def _make_router(self):
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        return SmartRouter(cfg)

    def test_callback_not_fired_before_routing(self):
        """Callback must fire after _route(), so page_class is known."""
        router = self._make_router()
        call_order = []

        def cb(page_num, total_pages, page_class, model_used, char_count):
            call_order.append(("cb", page_num))

        page = MagicMock()
        profile = _profile(PageClass.PURE_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route") as mock_route:
            mock_analyzer.analyze.side_effect = lambda p: (call_order.append(("analyze",)) or profile)
            mock_route.side_effect = lambda *a, **kw: (call_order.append(("route",)) or [])
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=1)
                mock_open.return_value = doc
                router.extract_pdf(MagicMock(), progress_callback=cb)

        # callback must come after route
        route_idx = call_order.index(("route",))
        cb_idx = call_order.index(("cb", 1))
        assert cb_idx > route_idx

    def test_callback_pure_text_args(self):
        """PURE_TEXT page: model_used='skip', char_count=0."""
        router = self._make_router()
        received = []

        def cb(page_num, total_pages, page_class, model_used, char_count):
            received.append((page_num, total_pages, page_class, model_used, char_count))

        page = MagicMock()
        profile = _profile(PageClass.PURE_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", return_value=[]):
            mock_analyzer.analyze.return_value = profile
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=3)
                mock_open.return_value = doc
                router.extract_pdf(MagicMock(), progress_callback=cb)

        assert len(received) == 1
        page_num, total_pages, page_class, model_used, char_count = received[0]
        assert page_num == 1
        assert total_pages == 3
        assert page_class == "pure_text"
        assert model_used == "skip"
        assert char_count == 0

    def test_callback_structured_text_args(self):
        """STRUCTURED_TEXT page: model_used='glm-ocr', char_count=len of extracted text."""
        router = self._make_router()
        received = []

        def cb(page_num, total_pages, page_class, model_used, char_count):
            received.append((page_num, total_pages, page_class, model_used, char_count))

        chunk = {
            "doc_type": "image_description",
            "page_num": 1,
            "image_index": 0,
            "text": "extracted text here",
            "model_used": "glm-ocr",
            "page_class": "structured_text",
            "content_type": "structured_text",
        }
        page = MagicMock()
        profile = _profile(PageClass.STRUCTURED_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", return_value=[chunk]):
            mock_analyzer.analyze.return_value = profile
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=1)
                mock_open.return_value = doc
                router.extract_pdf(MagicMock(), progress_callback=cb)

        assert len(received) == 1
        _, _, page_class, model_used, char_count = received[0]
        assert page_class == "structured_text"
        assert model_used == "glm-ocr"
        assert char_count == len("extracted text here")

    def test_callback_exception_does_not_abort_extraction(self):
        """Exception inside callback must not propagate or stop processing."""
        router = self._make_router()

        def bad_cb(*args):
            raise ValueError("oops")

        page1, page2 = MagicMock(), MagicMock()
        profile = _profile(PageClass.PURE_TEXT)
        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", return_value=[]):
            mock_analyzer.analyze.return_value = profile
            with patch("fitz.open") as mock_open:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
                doc.__len__ = MagicMock(return_value=2)
                mock_open.return_value = doc
                # Must not raise
                result = router.extract_pdf(MagicMock(), progress_callback=bad_cb)
        assert result == []
