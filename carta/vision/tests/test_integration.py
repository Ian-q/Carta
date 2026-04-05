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
