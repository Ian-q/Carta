"""Tests for dual extraction router."""
import pytest
from unittest.mock import MagicMock, patch, call
from pathlib import Path

from carta.vision.router import (
    DualExtractionRouter,
    ExtractionResult,
    extract_pdf_with_intelligent_routing
)
from carta.vision.classifier import ContentType


class TestDualExtractionRouterInitialization:
    """Router initialization from config."""
    
    def test_default_initialization(self):
        """Router initializes with default config values."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ollama_model": "nomic-embed-text:latest",
                "ollama_vision_model": "llava:latest",
                "ocr_model": "glm-ocr:latest",
            }
        }
        
        router = DualExtractionRouter(cfg)
        
        assert router.ocr_model == "glm-ocr:latest"
        assert router.vision_model == "llava:latest"
        assert router.ollama_url == "http://localhost:11434"
        assert router.vision_routing == "auto"
        assert router.classifier.text_threshold == 0.70
        assert router.classifier.visual_threshold == 0.40
    
    def test_custom_thresholds_from_config(self):
        """Router reads custom thresholds from config."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "classification": {
                    "text_threshold": 0.80,
                    "visual_threshold": 0.50,
                },
                "vision_routing": "ocr",  # Force OCR mode
            }
        }
        
        router = DualExtractionRouter(cfg)
        
        assert router.classifier.text_threshold == 0.80
        assert router.classifier.visual_threshold == 0.50
        assert router.vision_routing == "ocr"
    
    def test_missing_ocr_model_defaults_to_glm_ocr(self):
        """If ocr_model not in config, defaults to glm-ocr:latest."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
            }
        }
        
        router = DualExtractionRouter(cfg)
        
        assert router.ocr_model == "glm-ocr:latest"
        assert router.vision_model == "llava:latest"  # Also default


class TestDualExtractionRouterAutoRouting:
    """Automatic routing based on content classification."""
    
    def test_text_page_routes_to_ocr(self):
        """TEXT pages are routed to GLM-OCR."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        with patch.object(router.classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.TEXT,
                has_tables=True,
                confidence=0.90
            )
            
            with patch.object(router, '_call_ollama_vision') as mock_call:
                mock_call.return_value = "Extracted text content"
                
                result = router.extract_page(mock_page, page_num=1)
        
        assert result.model_used == "glm-ocr"
        assert result.content_type == "text"
        assert result.has_tables is True
        mock_call.assert_called_once()
        # Verify OCR model was used
        call_args = mock_call.call_args
        assert call_args[1]['model'] == "glm-ocr:latest"
    
    def test_visual_page_routes_to_vision(self):
        """VISUAL pages are routed to LLaVA."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        with patch.object(router.classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.VISUAL,
                has_tables=False,
                confidence=0.85
            )
            
            with patch.object(router, '_call_ollama_vision') as mock_call:
                mock_call.return_value = "Visual description of diagram"
                
                result = router.extract_page(mock_page, page_num=2)
        
        assert result.model_used == "llava"
        assert result.content_type == "visual"
        assert result.has_tables is False
        # Verify vision model was used
        call_args = mock_call.call_args
        assert call_args[1]['model'] == "llava:latest"
    
    def test_mixed_page_routes_to_hybrid(self):
        """MIXED pages use hybrid extraction."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        with patch.object(router.classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.MIXED,
                has_tables=False,
                confidence=0.75
            )
            
            with patch.object(router, '_call_ollama_vision') as mock_call:
                # Both calls succeed
                mock_call.side_effect = [
                    "OCR text content",
                    "Visual description"
                ]
                
                result = router.extract_page(mock_page, page_num=3)
        
        assert result.model_used == "hybrid"
        assert result.content_type == "mixed"
        # Both models should have been called
        assert mock_call.call_count == 2


class TestForcedRoutingModes:
    """Forced routing modes override auto-classification."""
    
    def test_force_ocr_mode(self):
        """force_mode='ocr' always uses GLM-OCR."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        # Classifier would say VISUAL, but we force OCR
        with patch.object(router.classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.VISUAL,
                has_tables=False,
                confidence=0.90
            )
            
            with patch.object(router, '_call_ollama_vision') as mock_call:
                mock_call.return_value = "OCR result"
                
                result = router.extract_page(mock_page, page_num=1, force_mode="ocr")
        
        assert result.model_used == "glm-ocr"
        # Should have used OCR despite classification saying VISUAL
        call_args = mock_call.call_args
        assert call_args[1]['model'] == "glm-ocr:latest"
    
    def test_force_vision_mode(self):
        """force_mode='vision' always uses LLaVA."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        # Classifier would say TEXT, but we force vision
        with patch.object(router.classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.TEXT,
                has_tables=True,
                confidence=0.90
            )
            
            with patch.object(router, '_call_ollama_vision') as mock_call:
                mock_call.return_value = "Vision result"
                
                result = router.extract_page(mock_page, page_num=1, force_mode="vision")
        
        assert result.model_used == "llava"
        call_args = mock_call.call_args
        assert call_args[1]['model'] == "llava:latest"
    
    def test_force_both_mode(self):
        """force_mode='both' always uses hybrid extraction."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        with patch.object(router.classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.TEXT,  # Would normally use OCR only
                has_tables=True,
                confidence=0.90
            )
            
            with patch.object(router, '_call_ollama_vision') as mock_call:
                mock_call.side_effect = ["OCR result", "Vision result"]
                
                result = router.extract_page(mock_page, page_num=1, force_mode="both")
        
        assert result.model_used == "hybrid"
        assert result.content_type == "mixed"
        assert mock_call.call_count == 2


class TestHybridExtractionCombinations:
    """Hybrid extraction handles various success/failure scenarios."""
    
    def test_hybrid_both_succeed(self):
        """When both models succeed, combine their outputs."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        with patch.object(router, '_call_ollama_vision') as mock_call:
            mock_call.side_effect = [
                "OCR: Temperature range -40°C to 85°C",
                "Vision: Graph showing temperature curve with steep drop at cold end"
            ]
            
            result = router._extract_hybrid(mock_page, page_num=1, classification=None)
        
        assert result.model_used == "hybrid"
        assert "Temperature range" in result.text
        assert "Visual Context" in result.text
        assert "temperature curve" in result.text
        assert result.confidence == 0.90
    
    def test_hybrid_ocr_fails_fallback_to_vision(self):
        """When OCR fails but vision succeeds, use vision only."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        with patch.object(router, '_call_ollama_vision') as mock_call:
            # First call (OCR) fails, second (vision) succeeds
            mock_call.side_effect = [
                Exception("OCR failed"),
                "Vision description only"
            ]
            
            result = router._extract_hybrid(mock_page, page_num=1, classification=None)
        
        assert result.model_used == "llava"  # Falls back to vision
        assert result.text == "Vision description only"
        assert result.confidence == 0.75  # Lower confidence
    
    def test_hybrid_both_fail(self):
        """When both models fail, return error message."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = b"fake_png"
        mock_page.get_pixmap.return_value = mock_pixmap
        
        with patch.object(router, '_call_ollama_vision') as mock_call:
            mock_call.side_effect = Exception("Both failed")
            
            result = router._extract_hybrid(mock_page, page_num=5, classification=None)
        
        assert result.model_used == "hybrid"
        assert "failed" in result.text.lower()
        assert result.confidence == 0.0


class TestExtractionResultStructure:
    """ExtractionResult data structure."""
    
    def test_result_has_required_fields(self):
        """ExtractionResult contains all required fields."""
        result = ExtractionResult(
            text="Extracted content",
            model_used="glm-ocr",
            content_type="text",
            page_num=1,
            confidence=0.90,
            has_tables=True
        )
        
        assert result.text == "Extracted content"
        assert result.model_used == "glm-ocr"
        assert result.content_type == "text"
        assert result.page_num == 1
        assert result.confidence == 0.90
        assert result.has_tables is True
    
    def test_optional_has_tables_defaults_false(self):
        """has_tables defaults to False when not specified."""
        result = ExtractionResult(
            text="Content",
            model_used="llava",
            content_type="visual",
            page_num=2,
            confidence=0.85
        )
        
        assert result.has_tables is False


class TestPDFExtractionIntegration:
    """Full PDF extraction integration."""
    
    @pytest.mark.skip(reason="Requires actual fitz and file system")
    def test_extract_pdf_processes_all_pages(self):
        """extract_pdf processes each page of a PDF."""
        # This would require mocking fitz.Document extensively
        pass
    
    def test_progress_callback_called(self):
        """Progress callback is invoked for each page."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        # Mock fitz
        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=3)
        mock_doc.__iter__ = MagicMock(return_value=iter([MagicMock(), MagicMock(), MagicMock()]))
        
        progress_calls = []
        
        def progress_cb(page_num, total):
            progress_calls.append((page_num, total))
        
        with patch('carta.vision.router.fitz') as mock_fitz:
            mock_fitz.open.return_value = mock_doc
            
            with patch.object(router, 'extract_page') as mock_extract:
                mock_extract.return_value = ExtractionResult(
                    text="test", model_used="ocr", content_type="text",
                    page_num=1, confidence=0.9
                )
                
                router.extract_pdf(Path("test.pdf"), progress_callback=progress_cb)
        
        # Should have been called for each of 3 pages
        assert len(progress_calls) == 3
        assert progress_calls == [(1, 3), (2, 3), (3, 3)]
    
    def test_progress_callback_errors_dont_stop(self):
        """Errors in progress callback don't stop extraction."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        router = DualExtractionRouter(cfg)
        
        mock_doc = MagicMock()
        mock_doc.__len__ = MagicMock(return_value=2)
        mock_doc.__iter__ = MagicMock(return_value=iter([MagicMock(), MagicMock()]))
        
        def failing_cb(page_num, total):
            raise Exception("Callback failed")
        
        with patch('carta.vision.router.fitz') as mock_fitz:
            mock_fitz.open.return_value = mock_doc
            
            with patch.object(router, 'extract_page') as mock_extract:
                mock_extract.return_value = ExtractionResult(
                    text="test", model_used="ocr", content_type="text",
                    page_num=1, confidence=0.9
                )
                
                # Should not raise despite callback error
                results = router.extract_pdf(Path("test.pdf"), progress_callback=failing_cb)
        
        assert len(results) == 2  # Both pages still processed


class TestConvenienceFunction:
    """Convenience function extract_pdf_with_intelligent_routing."""
    
    def test_convenience_function_creates_router(self):
        """Convenience function creates router and extracts."""
        cfg = {"embed": {"ollama_url": "http://localhost:11434"}}
        
        with patch('carta.vision.router.DualExtractionRouter') as mock_router_cls:
            mock_router = MagicMock()
            mock_router.extract_pdf.return_value = [
                ExtractionResult(text="p1", model_used="ocr", content_type="text", page_num=1, confidence=0.9)
            ]
            mock_router_cls.return_value = mock_router
            
            results = extract_pdf_with_intelligent_routing(Path("test.pdf"), cfg)
        
        mock_router_cls.assert_called_once_with(cfg)
        mock_router.extract_pdf.assert_called_once()
        assert len(results) == 1
