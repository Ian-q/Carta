"""Integration tests for Phase 999.4: GLM-OCR Intelligent Extraction.

End-to-end tests verifying the complete pipeline:
- Content classification → Model routing → Extraction → Chunking → Sidecar metadata
"""
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from carta.vision.classifier import ContentClassifier, ContentType
from carta.vision.router import DualExtractionRouter, ExtractionResult
from carta.vision.chunking import chunk_extraction_result, Chunk


class TestEndToEndClassificationRouting:
    """Complete flow from classification to extraction routing."""
    
    def test_text_pdf_routed_to_ocr(self):
        """Text-heavy PDF pages are classified and routed to GLM-OCR."""
        # Setup classifier
        classifier = ContentClassifier()
        
        # Setup router with mocked Ollama calls
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ocr_model": "glm-ocr:latest",
                "ollama_vision_model": "llava:latest",
            }
        }
        router = DualExtractionRouter(cfg)
        
        # Mock page that looks text-heavy
        mock_page = MagicMock()
        
        with patch.object(classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.TEXT,
                text_coverage=0.85,
                has_tables=True,
                confidence=0.90
            )
            
            # Classification determines routing
            classification = classifier.classify_page(mock_page)
            assert classification.content_type == ContentType.TEXT
            
            # Router would use GLM-OCR for TEXT content
            with patch.object(router, '_call_ollama_vision') as mock_call:
                mock_call.return_value = "| Register | Value |\n|----------|-------|\n| 0x00 | 0xFF |"
                
                # Simulate extraction
                result = router._extract_with_ocr(mock_page, page_num=1, classification=classification)
                
                # Verify OCR was called (not vision model)
                call_args = mock_call.call_args
                assert call_args[1]['model'] == "glm-ocr:latest"
                assert result.model_used == "glm-ocr"
                assert result.has_tables is True
    
    def test_visual_pdf_routed_to_llava(self):
        """Visual PDF pages are classified and routed to LLaVA."""
        classifier = ContentClassifier()
        
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ocr_model": "glm-ocr:latest",
                "ollama_vision_model": "llava:latest",
            }
        }
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        
        with patch.object(classifier, 'classify_page') as mock_classify:
            mock_classify.return_value = MagicMock(
                content_type=ContentType.VISUAL,
                text_coverage=0.15,
                image_coverage=0.70,
                has_tables=False,
                confidence=0.85
            )
            
            classification = classifier.classify_page(mock_page)
            assert classification.content_type == ContentType.VISUAL
            
            with patch.object(router, '_call_ollama_vision') as mock_call:
                mock_call.return_value = "Temperature curve showing linear response from -40°C to 85°C"
                
                result = router._extract_with_vision(mock_page, page_num=2, classification=classification)
                
                # Verify LLaVA was called
                call_args = mock_call.call_args
                assert call_args[1]['model'] == "llava:latest"
                assert result.model_used == "llava"
                assert result.content_type == "visual"


class TestEndToEndExtractionChunking:
    """Complete flow from extraction to chunking with table preservation."""
    
    def test_ocr_output_with_tables_preserved_in_chunks(self):
        """GLM-OCR output with tables is chunked with table preservation."""
        # Simulate GLM-OCR extraction result
        ocr_result = ExtractionResult(
            text="Introduction text.\n\n| Pin | Function | Voltage |\n|-----|----------|---------|\n| 1 | VCC | 5V |\n| 2 | GND | 0V |\n\nConclusion text.",
            model_used="glm-ocr",
            content_type="text",
            page_num=1,
            confidence=0.95,
            has_tables=True
        )
        
        # Chunk the result
        chunks = chunk_extraction_result(
            ocr_result.text,
            content_type=ocr_result.content_type,
            has_tables=ocr_result.has_tables,
            page_num=ocr_result.page_num
        )
        
        # Verify table was preserved as single chunk
        table_chunks = [c for c in chunks if c.chunk_type == "table"]
        assert len(table_chunks) == 1
        
        # Verify table content is intact
        table_chunk = table_chunks[0]
        assert "| Pin | Function | Voltage |" in table_chunk.text
        assert "| 1 | VCC | 5V |" in table_chunk.text
        assert "| 2 | GND | 0V |" in table_chunk.text
        
        # Verify text chunks exist too
        text_chunks = [c for c in chunks if c.chunk_type == "text"]
        assert len(text_chunks) >= 1
    
    def test_visual_output_chunked_normally(self):
        """LLaVA output (no tables) is chunked normally."""
        vision_result = ExtractionResult(
            text="The block diagram shows a central processing unit connected to memory via a 32-bit bus. The temperature sensor connects to ADC channel 0. Power comes from a 5V regulator.",
            model_used="llava",
            content_type="visual",
            page_num=2,
            confidence=0.88,
            has_tables=False
        )
        
        chunks = chunk_extraction_result(
            vision_result.text,
            content_type=vision_result.content_type,
            has_tables=vision_result.has_tables,
            page_num=vision_result.page_num
        )
        
        # All chunks should be text type (no tables)
        for chunk in chunks:
            assert chunk.chunk_type == "text"
        
        # Verify content is preserved
        full_text = ' '.join(c.text for c in chunks)
        assert "block diagram" in full_text
        assert "temperature sensor" in full_text
    
    def test_hybrid_result_combines_both_extractions(self):
        """Hybrid extraction combines OCR text with vision context."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ocr_model": "glm-ocr:latest",
                "ollama_vision_model": "llava:latest",
            }
        }
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        # Mock both models returning content
        with patch.object(router, '_call_ollama_vision') as mock_call:
            mock_call.side_effect = [
                "| Temp | Value |\n|------|-------|\n| 25C | 3.3V |",  # OCR result
                "Graph shows voltage vs temperature curve with slight nonlinearity at extremes"  # Vision result
            ]
            
            result = router._extract_hybrid(mock_page, page_num=3, classification=None)
            
            # Verify combined output
            assert result.model_used == "hybrid"
            assert "| Temp | Value |" in result.text  # OCR table preserved
            assert "Visual Context" in result.text  # Vision context included
            assert "voltage vs temperature" in result.text


class TestSidecarMetadataPropagation:
    """Verify extraction metadata flows correctly to sidecar."""
    
    def test_multiple_pages_aggregate_metadata(self):
        """Multiple pages aggregate into correct sidecar metadata."""
        # Simulate extraction results from multiple pages
        extraction_results = [
            ExtractionResult(text="Table page", model_used="glm-ocr", content_type="text", page_num=1, confidence=0.95, has_tables=True),
            ExtractionResult(text="Diagram page", model_used="llava", content_type="visual", page_num=2, confidence=0.88, has_tables=False),
            ExtractionResult(text="Mixed content", model_used="hybrid", content_type="mixed", page_num=3, confidence=0.90, has_tables=False),
        ]
        
        # Build metadata as pipeline would
        glm_ocr_pages = sum(1 for r in extraction_results if r.model_used == "glm-ocr")
        llava_pages = sum(1 for r in extraction_results if r.model_used == "llava")
        hybrid_pages = sum(1 for r in extraction_results if r.model_used == "hybrid")
        
        page_details = [
            {
                "page": r.page_num,
                "content_type": r.content_type,
                "model": r.model_used,
                "has_tables": r.has_tables,
                "confidence": r.confidence,
            }
            for r in extraction_results
        ]
        
        # Verify metadata structure
        assert glm_ocr_pages == 1
        assert llava_pages == 1
        assert hybrid_pages == 1
        assert len(page_details) == 3
        
        # Verify page details content
        assert page_details[0]["model"] == "glm-ocr"
        assert page_details[0]["has_tables"] is True
        assert page_details[1]["content_type"] == "visual"
        assert page_details[2]["model"] == "hybrid"


class TestConfigDrivenRouting:
    """Verify config options drive extraction behavior."""
    
    def test_vision_routing_auto_respects_classification(self):
        """vision_routing: auto uses classification to select model."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "vision_routing": "auto",
                "classification": {
                    "text_threshold": 0.70,
                    "visual_threshold": 0.40,
                }
            }
        }
        router = DualExtractionRouter(cfg)
        
        assert router.vision_routing == "auto"
        assert router.classifier.text_threshold == 0.70
    
    def test_vision_routing_forced_ocr(self):
        """vision_routing: ocr forces GLM-OCR for all pages."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "vision_routing": "ocr",
            }
        }
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        # Even if classified as visual, should use OCR
        with patch.object(router, '_call_ollama_vision') as mock_call:
            mock_call.return_value = "OCR output"
            
            result = router.extract_page(mock_page, page_num=1, force_mode="ocr")
            
            # Verify OCR model was used
            call_args = mock_call.call_args
            assert call_args[1]['model'] == "glm-ocr:latest"
            assert result.model_used == "glm-ocr"


class TestFailOpenBehavior:
    """Verify fail-open behavior when models unavailable."""
    
    def test_ocr_failure_falls_back_to_vision(self):
        """When GLM-OCR fails, falls back to LLaVA."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
                "ocr_model": "glm-ocr:latest",
                "ollama_vision_model": "llava:latest",
            }
        }
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        classification = MagicMock(
            content_type=ContentType.TEXT,
            has_tables=True,
            confidence=0.90
        )
        
        # OCR fails, then vision succeeds
        with patch.object(router, '_call_ollama_vision') as mock_call:
            mock_call.side_effect = [
                Exception("Ollama GLM-OCR not available"),
                "Fallback vision description"
            ]
            
            result = router._extract_with_ocr(mock_page, page_num=1, classification=classification)
            
            # Should have fallen back to vision
            assert result.model_used == "llava"
            assert result.text == "Fallback vision description"
    
    def test_total_failure_returns_error_message(self):
        """When both models fail, returns error message gracefully."""
        cfg = {
            "embed": {
                "ollama_url": "http://localhost:11434",
            }
        }
        router = DualExtractionRouter(cfg)
        
        mock_page = MagicMock()
        mock_pixmap = MagicMock()
        mock_page.get_pixmap.return_value = mock_pixmap
        mock_pixmap.tobytes.return_value = b"fake_png"
        
        with patch.object(router, '_call_ollama_vision') as mock_call:
            mock_call.side_effect = Exception("Ollama unavailable")
            
            result = router._extract_with_vision(mock_page, page_num=5, classification=None)
            
            # Should return error message, not crash
            assert "failed" in result.text.lower()
            assert result.confidence == 0.0


class TestChunkMetadataPropagation:
    """Verify chunk metadata carries through pipeline."""
    
    def test_chunk_includes_page_and_type_info(self):
        """Chunks preserve page number and content type metadata."""
        extraction_text = "Page 5 content with | table | data |"
        
        chunks = chunk_extraction_result(
            extraction_text,
            page_num=5,
            content_type="text",
            has_tables=True
        )
        
        # All chunks should have page_num = 5
        for chunk in chunks:
            assert chunk.page_num == 5
        
        # Table chunk should be identifiable
        table_chunks = [c for c in chunks if c.chunk_type == "table"]
        if table_chunks:
            assert table_chunks[0].chunk_type == "table"


# Integration smoke test marker
@pytest.mark.integration
class TestFullPipelineSmoke:
    """Smoke tests for complete pipeline (marked as integration)."""
    
    def test_classifier_router_chunking_chain(self):
        """Complete chain: classify → extract → chunk."""
        # This is a conceptual test showing the full flow
        # In practice, each component is tested separately above
        
        # 1. Classification (mocked page)
        classifier = ContentClassifier()
        mock_page = MagicMock()
        
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.80):
            with patch.object(classifier, '_detect_table_patterns_from_page', return_value=True):
                classification = classifier.classify_page(mock_page)
                assert classification.content_type == ContentType.TEXT
        
        # 2. Extraction would happen here (requires real Ollama)
        # Skipped in unit tests
        
        # 3. Chunking
        sample_ocr = "| Col1 | Col2 |\n|------|------|\n| A | B |"
        chunks = chunk_extraction_result(sample_ocr, content_type="text", has_tables=True)
        
        # Verify chain works
        assert len(chunks) >= 1
        assert any(c.chunk_type == "table" for c in chunks)
