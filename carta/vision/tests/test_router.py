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
# Config defaults
# ---------------------------------------------------------------------------

class TestVisionModelDefault:
    def test_defaults_to_current_vision_model(self):
        """When config omits ollama_vision_model, SmartRouter falls back to the
        current default (qwen3-vl:8b) — not the retired qwen3-vl:8b (#56)."""
        router = SmartRouter(_cfg())
        assert router.vision_model == "qwen3-vl:8b"


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
    def _mock_stream(self, lines: list[bytes], status_code: int = 200):
        """Return a mock requests.Response usable as a context manager
        (the code now does `with requests.post(...) as resp:` to release the
        streamed connection)."""
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.iter_lines.return_value = iter(lines)
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        return mock_resp

    def test_accumulates_streamed_tokens_into_response(self):
        """Streaming chunks are joined; stream=True sent to Ollama."""
        router = SmartRouter(_cfg())
        lines = [
            b'{"response": "Hello", "done": false}',
            b'{"response": " world", "done": false}',
            b'{"response": "", "done": true}',
        ]
        with patch("carta.vision.router.requests") as mock_requests:
            mock_requests.post.return_value = self._mock_stream(lines)
            result = router._call_ollama_vision(b"fakepng", model="glm-ocr:latest", prompt="extract")
        assert result == "Hello world"
        assert mock_requests.post.call_args[1]["json"]["stream"] is True
        assert mock_requests.post.call_args[1]["stream"] is True

    def test_returns_stripped_response(self):
        router = SmartRouter(_cfg())
        lines = [
            b'{"response": "  description  ", "done": false}',
            b'{"response": "", "done": true}',
        ]
        with patch("carta.vision.router.requests") as mock_requests:
            mock_requests.post.return_value = self._mock_stream(lines)
            result = router._call_ollama_vision(b"fakepng", model="llava", prompt="describe")
        assert result == "description"

    def test_raises_on_non_200(self):
        router = SmartRouter(_cfg())
        with patch("carta.vision.router.requests") as mock_requests:
            mock_requests.post.return_value = self._mock_stream([], status_code=503)
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
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=1)
                mock_fitz.open.return_value = doc
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
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=3)
                mock_fitz.open.return_value = doc
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
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page]))
                doc.__len__ = MagicMock(return_value=1)
                mock_fitz.open.return_value = doc
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
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
                doc.__len__ = MagicMock(return_value=2)
                mock_fitz.open.return_value = doc
                # Must not raise
                result = router.extract_pdf(MagicMock(), progress_callback=bad_cb)
        assert result == []


# ---------------------------------------------------------------------------
# Parallel page extraction (vision_workers > 1)
# ---------------------------------------------------------------------------

class TestParallelExtraction:
    def test_workers_1_processes_all_pages_serially(self):
        """vision_workers=1 must still call _route for every page."""
        router = SmartRouter(_cfg(vision_workers=1))
        profile = _profile(PageClass.STRUCTURED_TEXT)
        pages = [MagicMock(name=f"p{i}") for i in range(5)]
        chunks_seen = []

        def fake_route(page, page_num, profile, doc):
            chunks_seen.append(page_num)
            return [{"doc_type": "image_description", "page_num": page_num,
                     "image_index": 0, "text": f"page {page_num}", "model_used": "glm-ocr",
                     "page_class": "structured_text", "content_type": "structured_text"}]

        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", side_effect=fake_route):
            mock_analyzer.analyze.return_value = profile
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter(pages))
                doc.__len__ = MagicMock(return_value=len(pages))
                mock_fitz.open.return_value = doc
                results = router.extract_pdf(MagicMock())

        assert sorted(chunks_seen) == [1, 2, 3, 4, 5]
        # Results must come back in page order regardless of completion order
        assert [r["page_num"] for r in results] == [1, 2, 3, 4, 5]

    def test_workers_4_processes_all_pages_and_preserves_order(self):
        """vision_workers=4 must call _route once per page; results returned in page order."""
        router = SmartRouter(_cfg(vision_workers=4))
        profile = _profile(PageClass.STRUCTURED_TEXT)
        pages = [MagicMock(name=f"p{i}") for i in range(8)]
        seen = set()
        seen_lock = __import__("threading").Lock()

        def fake_route(page, page_num, profile, doc):
            with seen_lock:
                seen.add(page_num)
            return [{"doc_type": "image_description", "page_num": page_num,
                     "image_index": 0, "text": f"page {page_num}", "model_used": "glm-ocr",
                     "page_class": "structured_text", "content_type": "structured_text"}]

        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", side_effect=fake_route):
            mock_analyzer.analyze.return_value = profile
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter(pages))
                doc.__len__ = MagicMock(return_value=len(pages))
                mock_fitz.open.return_value = doc
                results = router.extract_pdf(MagicMock())

        assert seen == {1, 2, 3, 4, 5, 6, 7, 8}
        assert [r["page_num"] for r in results] == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_parallel_calls_overlap_in_time(self):
        """vision_workers=4 must execute _route concurrently, not serially."""
        import threading
        import time

        router = SmartRouter(_cfg(vision_workers=4))
        profile = _profile(PageClass.STRUCTURED_TEXT)
        pages = [MagicMock(name=f"p{i}") for i in range(4)]

        in_flight = 0
        peak_in_flight = 0
        lock = threading.Lock()

        def slow_route(page, page_num, profile, doc):
            nonlocal in_flight, peak_in_flight
            with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            return []

        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", side_effect=slow_route):
            mock_analyzer.analyze.return_value = profile
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter(pages))
                doc.__len__ = MagicMock(return_value=len(pages))
                mock_fitz.open.return_value = doc
                router.extract_pdf(MagicMock())

        assert peak_in_flight >= 2, f"expected concurrent _route calls, got peak={peak_in_flight}"


# ---------------------------------------------------------------------------
# Vision checkpoint helpers + per-page resume
# ---------------------------------------------------------------------------

class TestCheckpointHelpers:
    def test_load_returns_empty_when_path_is_none(self):
        from carta.vision.router import load_vision_checkpoint
        assert load_vision_checkpoint(None, "vm", "om") == {}

    def test_load_returns_empty_when_file_missing(self, tmp_path):
        from carta.vision.router import load_vision_checkpoint
        assert load_vision_checkpoint(tmp_path / "nope.json", "vm", "om") == {}

    def test_load_returns_empty_on_corrupt_file(self, tmp_path):
        from carta.vision.router import load_vision_checkpoint
        p = tmp_path / "cp.json"
        p.write_text("{not-json")
        assert load_vision_checkpoint(p, "vm", "om") == {}

    def test_load_drops_stale_checkpoint_on_model_swap(self, tmp_path):
        from carta.vision.router import save_vision_checkpoint, load_vision_checkpoint
        p = tmp_path / "cp.json"
        save_vision_checkpoint(p, Path("/x.pdf"), "old-vision", "old-ocr",
                               [{"page_num": 1, "chunks": [{"text": "x"}]}])
        # Reload with different model — checkpoint is invalidated and removed.
        out = load_vision_checkpoint(p, "new-vision", "old-ocr")
        assert out == {}
        assert not p.exists()

    def test_save_then_load_roundtrip(self, tmp_path):
        from carta.vision.router import save_vision_checkpoint, load_vision_checkpoint
        p = tmp_path / "cp.json"
        chunks = [{"text": "page 1 text", "model_used": "glm-ocr"}]
        save_vision_checkpoint(p, Path("/x.pdf"), "qwen3-vl:8b", "glm-ocr:latest",
                               [{"page_num": 1, "chunks": chunks}])
        out = load_vision_checkpoint(p, "qwen3-vl:8b", "glm-ocr:latest")
        assert out == {1: chunks}

    def test_save_is_atomic_no_temp_files_left(self, tmp_path):
        from carta.vision.router import save_vision_checkpoint
        p = tmp_path / "cp.json"
        save_vision_checkpoint(p, Path("/x.pdf"), "v", "o",
                               [{"page_num": 1, "chunks": []}])
        # Only the final file should exist; tempfiles must be renamed away.
        assert sorted(x.name for x in tmp_path.iterdir()) == ["cp.json"]


class TestExtractPdfResume:
    def test_resume_skips_pages_in_checkpoint(self, tmp_path):
        from carta.vision.router import SmartRouter, save_vision_checkpoint

        router = SmartRouter(_cfg(ollama_vision_model="qwen3-vl:8b",
                                  ocr_model="glm-ocr:latest"))
        cp = tmp_path / "cp.json"
        prior = [
            {"doc_type": "image_description", "page_num": 1, "image_index": 0,
             "text": "cached p1", "model_used": "glm-ocr",
             "page_class": "structured_text", "content_type": "structured_text"},
        ]
        save_vision_checkpoint(cp, Path("/x.pdf"), "qwen3-vl:8b", "glm-ocr:latest",
                               [{"page_num": 1, "chunks": prior}])

        new_chunk = {"doc_type": "image_description", "page_num": 2, "image_index": 0,
                     "text": "fresh p2", "model_used": "glm-ocr",
                     "page_class": "structured_text", "content_type": "structured_text"}

        page1 = MagicMock(name="p1")
        page2 = MagicMock(name="p2")
        profile = _profile(PageClass.STRUCTURED_TEXT)
        route_calls = []

        def fake_route(page, page_num, profile, doc):
            route_calls.append(page_num)
            return [new_chunk] if page_num == 2 else []

        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", side_effect=fake_route):
            mock_analyzer.analyze.return_value = profile
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
                doc.__len__ = MagicMock(return_value=2)
                mock_fitz.open.return_value = doc
                results = router.extract_pdf(Path("/x.pdf"), checkpoint_path=cp)

        # Page 1 was loaded from checkpoint; only page 2 was re-routed.
        assert route_calls == [2]
        # Output preserves both pages in order.
        assert [r["page_num"] for r in results] == [1, 2]
        assert results[0]["text"] == "cached p1"
        assert results[1]["text"] == "fresh p2"

    def test_checkpoint_is_updated_after_each_page(self, tmp_path):
        from carta.vision.router import SmartRouter, load_vision_checkpoint

        router = SmartRouter(_cfg(ollama_vision_model="qwen3-vl:8b",
                                  ocr_model="glm-ocr:latest",
                                  vision_workers=1))
        cp = tmp_path / "cp.json"
        page1 = MagicMock(name="p1")
        page2 = MagicMock(name="p2")
        profile = _profile(PageClass.STRUCTURED_TEXT)

        def fake_route(page, page_num, profile, doc):
            return [{"doc_type": "image_description", "page_num": page_num,
                     "image_index": 0, "text": f"page {page_num}",
                     "model_used": "glm-ocr",
                     "page_class": "structured_text",
                     "content_type": "structured_text"}]

        with patch.object(router, "analyzer") as mock_analyzer, \
             patch.object(router, "_route", side_effect=fake_route):
            mock_analyzer.analyze.return_value = profile
            with patch("carta.vision.router.fitz") as mock_fitz:
                doc = MagicMock()
                doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
                doc.__len__ = MagicMock(return_value=2)
                mock_fitz.open.return_value = doc
                router.extract_pdf(Path("/x.pdf"), checkpoint_path=cp)

        # After successful extraction, both pages are recorded in checkpoint.
        out = load_vision_checkpoint(cp, "qwen3-vl:8b", "glm-ocr:latest")
        assert sorted(out.keys()) == [1, 2]


# ---------------------------------------------------------------------------
# vision_routing mode tests (CHANGE 2)
# ---------------------------------------------------------------------------

def _cfg_routing(mode: str, **extra) -> dict:
    """Build a cfg dict with a specific vision_routing mode."""
    base = {
        "ollama_url": "http://localhost:11434",
        "vision_routing": mode,
        "ocr_model": "glm-ocr:latest",
        "ollama_vision_model": "llava:latest",
    }
    base.update(extra)
    return {"embed": base}


class TestVisionRoutingOff:
    def test_off_returns_empty_for_non_pure_text(self):
        """mode=off: _route always returns [] regardless of page class."""
        router = SmartRouter(_cfg_routing("off"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision") as mock_call:
            result = router._route(page, 1, _profile(PageClass.STRUCTURED_TEXT), MagicMock())
        assert result == []
        mock_call.assert_not_called()

    def test_off_returns_empty_for_text_with_images(self):
        router = SmartRouter(_cfg_routing("off"))
        page = MagicMock()
        with patch.object(router, "_call_ollama_vision") as mock_call:
            result = router._route(page, 2, _profile(PageClass.TEXT_WITH_IMAGES, has_images=True), MagicMock())
        assert result == []
        mock_call.assert_not_called()

    def test_off_returns_empty_for_flattened(self):
        router = SmartRouter(_cfg_routing("off"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision") as mock_call:
            result = router._route(page, 3, _profile(PageClass.FLATTENED), MagicMock())
        assert result == []
        mock_call.assert_not_called()


class TestVisionRoutingOcr:
    def test_ocr_routes_text_with_images_to_ocr_not_vlm(self):
        """mode=ocr: TEXT_WITH_IMAGES page uses OCR model, not vision model."""
        router = SmartRouter(_cfg_routing("ocr"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", return_value="ocr text") as mock_call:
            result = router._route(page, 1, _profile(PageClass.TEXT_WITH_IMAGES, has_images=True), MagicMock())
        # Should have called OCR via _route_structured (OCR path, not VLM)
        assert mock_call.call_count == 1
        assert mock_call.call_args[1]["model"] == "glm-ocr:latest"
        assert len(result) == 1
        assert result[0]["model_used"] == "glm-ocr"

    def test_ocr_routes_flattened_to_ocr(self):
        """mode=ocr: FLATTENED page also goes through OCR only."""
        router = SmartRouter(_cfg_routing("ocr"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", return_value="x" * 100) as mock_call:
            result = router._route(page, 1, _profile(PageClass.FLATTENED), MagicMock())
        # OCR mode forces _route_structured — one OCR call
        assert mock_call.call_count == 1
        assert mock_call.call_args[1]["model"] == "glm-ocr:latest"

    def test_ocr_still_skips_pure_text(self):
        """mode=ocr: PURE_TEXT pages are still skipped (zero model calls)."""
        router = SmartRouter(_cfg_routing("ocr"))
        page = MagicMock()
        with patch.object(router, "_call_ollama_vision") as mock_call:
            result = router._route(page, 1, _profile(PageClass.PURE_TEXT), MagicMock())
        assert result == []
        mock_call.assert_not_called()


class TestVisionRoutingVision:
    def test_vision_routes_structured_to_vlm_not_ocr(self):
        """mode=vision: STRUCTURED_TEXT page uses VLM, not OCR model."""
        router = SmartRouter(_cfg_routing("vision"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_extract_image_crops", return_value=[]):
            with patch.object(router, "_call_ollama_vision", return_value="vlm text") as mock_call:
                result = router._route(page, 1, _profile(PageClass.STRUCTURED_TEXT), MagicMock())
        # Should have called VLM via _route_text_with_images
        assert mock_call.call_count == 1
        assert mock_call.call_args[1]["model"] == "llava:latest"

    def test_vision_routes_text_with_images_to_vlm_not_ocr(self):
        """mode=vision: TEXT_WITH_IMAGES page uses VLM (_route_text_with_images), not OCR."""
        router = SmartRouter(_cfg_routing("vision"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_extract_image_crops", return_value=[]):
            with patch.object(router, "_call_ollama_vision", return_value="vlm desc") as mock_call:
                result = router._route(
                    page, 2, _profile(PageClass.TEXT_WITH_IMAGES, has_images=True), MagicMock()
                )
        # Must route via VLM (_route_text_with_images), not OCR
        assert mock_call.call_count == 1
        assert mock_call.call_args[1]["model"] == "llava:latest"

    def test_vision_still_skips_pure_text(self):
        """mode=vision: PURE_TEXT pages are still skipped."""
        router = SmartRouter(_cfg_routing("vision"))
        page = MagicMock()
        with patch.object(router, "_call_ollama_vision") as mock_call:
            result = router._route(page, 1, _profile(PageClass.PURE_TEXT), MagicMock())
        assert result == []
        mock_call.assert_not_called()


class TestVisionRoutingAuto:
    def test_auto_preserves_structured_text_ocr(self):
        """mode=auto: STRUCTURED_TEXT → OCR (unchanged from original behavior)."""
        router = SmartRouter(_cfg_routing("auto"))
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", return_value="table text") as mock_call:
            result = router._route(page, 1, _profile(PageClass.STRUCTURED_TEXT), MagicMock())
        assert mock_call.call_args[1]["model"] == "glm-ocr:latest"
        assert result[0]["model_used"] == "glm-ocr"

    def test_auto_preserves_text_with_images_vlm(self):
        """mode=auto: TEXT_WITH_IMAGES → VLM (unchanged from original behavior)."""
        router = SmartRouter(_cfg_routing("auto"))
        page = MagicMock()
        with patch.object(router, "_extract_image_crops", return_value=[(0, b"img")]):
            with patch.object(router, "_call_ollama_vision", return_value="img desc") as mock_call:
                result = router._route(page, 1, _profile(PageClass.TEXT_WITH_IMAGES, has_images=True), MagicMock())
        assert mock_call.call_args[1]["model"] == "llava:latest"
        assert result[0]["model_used"] == "llava"

    def test_auto_pure_text_still_zero_calls(self):
        """mode=auto: PURE_TEXT → [] with no model calls (unchanged)."""
        router = SmartRouter(_cfg_routing("auto"))
        page = MagicMock()
        with patch.object(router, "_call_ollama_vision") as mock_call:
            result = router._route(page, 1, _profile(PageClass.PURE_TEXT), MagicMock())
        assert result == []
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# vision_call_timeout_s threading tests (CHANGE 3)
# ---------------------------------------------------------------------------

class TestVisionCallTimeout:
    def test_default_timeout_is_300(self):
        """_call_ollama_vision signature default must be 300 (not 120)."""
        import inspect
        from carta.vision.router import SmartRouter as _SR
        sig = inspect.signature(_SR._call_ollama_vision)
        assert sig.parameters["timeout"].default == 300

    def test_configured_timeout_passed_to_call(self):
        """When vision_call_timeout_s=450, _call_ollama_vision receives timeout=450."""
        cfg = {"embed": {
            "ollama_url": "http://localhost:11434",
            "ocr_model": "glm-ocr:latest",
            "vision_call_timeout_s": 450,
        }}
        router = SmartRouter(cfg)
        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", return_value="text") as mock_call:
            router._route(page, 1, _profile(PageClass.STRUCTURED_TEXT), MagicMock())
        assert mock_call.call_args[1]["timeout"] == 450

    def test_default_config_uses_300_timeout(self):
        """When vision_call_timeout_s is not set, router defaults to 300."""
        cfg = {"embed": {
            "ollama_url": "http://localhost:11434",
            "ocr_model": "glm-ocr:latest",
        }}
        router = SmartRouter(cfg)
        assert router.vision_call_timeout == 300

        page = MagicMock()
        page.get_pixmap.return_value = _pixmap()
        with patch.object(router, "_call_ollama_vision", return_value="text") as mock_call:
            router._route(page, 1, _profile(PageClass.STRUCTURED_TEXT), MagicMock())
        assert mock_call.call_args[1]["timeout"] == 300


class TestResourceCleanup:
    """Long visual drains leak resources unless streamed responses and PDF
    handles are always released — even on early break / mid-page error."""

    def test_call_ollama_vision_closes_streamed_response(self):
        router = SmartRouter(_cfg())
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_lines.return_value = [b'{"response":"hi","done":true}']
        # requests.post(...) must be used as a context manager so urllib3 gets
        # the (undrained, early-broken) connection back.
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=resp)
        cm.__exit__ = MagicMock(return_value=False)
        with patch("carta.vision.router.requests.post", return_value=cm) as post:
            out = router._call_ollama_vision(b"PNG", "glm-ocr", "prompt")
        assert out == "hi"
        assert post.call_args.kwargs.get("stream") is True
        cm.__exit__.assert_called_once()  # response released even on early break

    def test_extract_pdf_closes_doc_when_a_page_raises(self):
        router = SmartRouter(_cfg())
        fake_doc = MagicMock()
        fake_doc.__len__ = MagicMock(return_value=1)
        fake_doc.__iter__ = MagicMock(return_value=iter([MagicMock()]))
        # analyzer.analyze raising mid-extraction must not leak the PDF handle.
        router.analyzer = MagicMock()
        router.analyzer.analyze.side_effect = RuntimeError("bad page")
        with patch("carta.vision.router.fitz") as fake_fitz:
            fake_fitz.open.return_value = fake_doc
            with pytest.raises(RuntimeError):
                router.extract_pdf(Path("/x/y.pdf"))
        fake_doc.close.assert_called_once()
