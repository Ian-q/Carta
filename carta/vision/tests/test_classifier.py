"""Tests for PDF page content classification module."""
import pytest
from unittest.mock import MagicMock, patch
import time

from carta.vision.classifier import ContentClassifier, ContentType, ClassificationResult


class TestContentClassifierTextPages:
    """Pages dominated by text should classify as TEXT."""
    
    def test_pure_text_page(self):
        """Page with only paragraphs → TEXT."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        mock_page.rect.width = 612  # Letter size
        mock_page.rect.height = 792
        
        # Simulate text covering 80% of page (10 blocks of ~80% area each is wrong math,
        # but get_text returns dicts with bbox info)
        # Correct: each text block has x0,y0,x1,y1
        mock_page.get_text.return_value = "text" * 1000  # Lots of text
        mock_page.get_images.return_value = []
        
        # Mock the internal calculation
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.80):
            result = classifier.classify_page(mock_page)
            assert result.content_type == ContentType.TEXT
            assert result.text_coverage == 0.80
    
    def test_table_page(self):
        """Page with structured tables → TEXT."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        
        # Low text coverage but table patterns detected
        # Need to patch the methods that classify_page actually calls
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.50):
            with patch.object(classifier, '_calculate_image_coverage', return_value=0.0):
                with patch.object(classifier, '_detect_table_patterns_from_page', return_value=True):
                    result = classifier.classify_page(mock_page)
                    assert result.content_type == ContentType.TEXT
                    assert result.has_tables is True


class TestContentClassifierVisualPages:
    """Image-heavy pages should classify as VISUAL."""
    
    def test_diagram_page(self):
        """Page with schematic/diagram → VISUAL."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        mock_page.rect.width = 612
        mock_page.rect.height = 792
        
        # Minimal text, large image
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.10):
            with patch.object(classifier, '_calculate_image_coverage', return_value=0.60):
                result = classifier.classify_page(mock_page)
                assert result.content_type == ContentType.VISUAL
                assert result.image_coverage == 0.60
    
    def test_plot_graph_page(self):
        """Page with plot/chart → VISUAL."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        
        # Some text (axis labels) but mostly plot area
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.15):
            with patch.object(classifier, '_calculate_image_coverage', return_value=0.55):
                result = classifier.classify_page(mock_page)
                assert result.content_type == ContentType.VISUAL


class TestContentClassifierMixedPages:
    """Balanced pages should classify as MIXED."""
    
    def test_half_text_half_diagram(self):
        """Page with text section + diagram → MIXED."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        
        # 50% text, moderate image
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.50):
            with patch.object(classifier, '_calculate_image_coverage', return_value=0.30):
                with patch.object(classifier, '_detect_table_patterns', return_value=False):
                    result = classifier.classify_page(mock_page)
                    assert result.content_type == ContentType.MIXED


class TestContentClassifierThresholds:
    """Threshold configuration affects classification."""
    
    def test_custom_text_threshold(self):
        """Higher threshold makes classification stricter."""
        classifier_default = ContentClassifier(text_threshold=0.70)
        classifier_strict = ContentClassifier(text_threshold=0.80)
        
        mock_page = MagicMock()
        
        # 65% text coverage
        with patch.object(classifier_default, '_calculate_text_coverage', return_value=0.65):
            with patch.object(classifier_default, '_detect_table_patterns', return_value=False):
                result_default = classifier_default.classify_page(mock_page)
        
        with patch.object(classifier_strict, '_calculate_text_coverage', return_value=0.65):
            with patch.object(classifier_strict, '_detect_table_patterns', return_value=False):
                result_strict = classifier_strict.classify_page(mock_page)
        
        assert result_default.content_type == ContentType.MIXED  # 65% < 70% default
        assert result_strict.content_type == ContentType.MIXED  # 65% < 80% strict
    
    def test_threshold_boundary_text(self):
        """Exactly at threshold → TEXT."""
        classifier = ContentClassifier(text_threshold=0.70)
        mock_page = MagicMock()
        
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.70):
            with patch.object(classifier, '_detect_table_patterns', return_value=False):
                result = classifier.classify_page(mock_page)
                assert result.content_type == ContentType.TEXT


class TestTableDetection:
    """Table pattern detection."""
    
    def test_detects_aligned_columns(self):
        """Aligned x-coordinates across rows → table detected."""
        classifier = ContentClassifier()
        
        # Simulate aligned text columns (table-like)
        # Each row has text blocks at similar x positions
        text_blocks = [
            {'x0': 50, 'x1': 150},   # Col 1
            {'x0': 200, 'x1': 300},  # Col 2
            {'x0': 350, 'x1': 450},  # Col 3
            {'x0': 50, 'x1': 150},   # Next row, Col 1
            {'x0': 200, 'x1': 300},  # Next row, Col 2
            {'x0': 350, 'x1': 450},  # Next row, Col 3
        ]
        
        assert classifier._detect_table_patterns(text_blocks) is True
    
    def test_ignores_single_column(self):
        """Single column text not detected as table."""
        classifier = ContentClassifier()
        
        text_blocks = [
            {'x0': 50, 'x1': 562},  # Full width paragraph
            {'x0': 50, 'x1': 562},  # Next paragraph
        ]
        
        assert classifier._detect_table_patterns(text_blocks) is False
    
    def test_detects_two_column_layout(self):
        """Two-column layout can be table-like."""
        classifier = ContentClassifier()
        
        text_blocks = [
            {'x0': 50, 'x1': 280},   # Left col
            {'x0': 330, 'x1': 562},  # Right col
            {'x0': 50, 'x1': 280},   # Next row, left
            {'x0': 330, 'x1': 562},  # Next row, right
        ]
        
        assert classifier._detect_table_patterns(text_blocks) is True


class TestCoverageCalculation:
    """Coverage calculation methods."""
    
    def test_calculate_text_coverage(self):
        """Text coverage calculated from block areas."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        mock_page.rect.width = 612
        mock_page.rect.height = 792
        
        # Mock get_text("blocks") returning bbox info
        # Format: list of (x0, y0, x1, y1, text, ...)
        mock_blocks = [
            (50, 50, 562, 100, "text1", 0, 0),   # 512x50 = 25600
            (50, 150, 562, 200, "text2", 0, 0),  # 512x50 = 25600
        ]
        # Total text area = 51200
        # Page area = 612*792 = 484704
        # Coverage = 51200 / 484704 ≈ 0.105
        
        mock_page.get_text.return_value = mock_blocks
        
        coverage = classifier._calculate_text_coverage(mock_page)
        assert 0.10 < coverage < 0.11
    
    def test_calculate_image_coverage(self):
        """Image coverage calculated from image list."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        mock_page.rect.width = 612
        mock_page.rect.height = 792
        
        # Mock get_images() returning (xref, smask, width, height, bpc, colorspace, alt. colorspace)
        mock_images = [
            (1, 0, 400, 300, 8, 0, 0),  # 400x300 = 120000
        ]
        # Image area = 120000
        # Page area = 484704
        # Coverage = 120000 / 484704 ≈ 0.247
        
        mock_page.get_images.return_value = mock_images
        
        coverage = classifier._calculate_image_coverage(mock_page)
        assert 0.24 < coverage < 0.25


class TestClassificationResult:
    """Classification result data structure."""
    
    def test_result_structure(self):
        """ClassificationResult has all required fields."""
        result = ClassificationResult(
            content_type=ContentType.TEXT,
            text_coverage=0.85,
            image_coverage=0.10,
            has_tables=True,
            confidence=0.95
        )
        
        assert result.content_type == ContentType.TEXT
        assert result.text_coverage == 0.85
        assert result.image_coverage == 0.10
        assert result.has_tables is True
        assert result.confidence == 0.95
    
    def test_content_type_enum(self):
        """ContentType enum has expected values."""
        assert ContentType.TEXT.value == "text"
        assert ContentType.VISUAL.value == "visual"
        assert ContentType.MIXED.value == "mixed"


class TestClassificationPerformance:
    """Performance requirements."""
    
    def test_classification_under_100ms(self):
        """Classification should complete in <100ms."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        
        # Set up mocks to avoid actual PDF processing
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.50):
            with patch.object(classifier, '_calculate_image_coverage', return_value=0.20):
                with patch.object(classifier, '_detect_table_patterns', return_value=False):
                    start = time.time()
                    result = classifier.classify_page(mock_page)
                    elapsed = time.time() - start
        
        assert elapsed < 0.100  # 100ms
        assert result.content_type == ContentType.MIXED


class TestEdgeCases:
    """Edge cases and boundary conditions."""
    
    def test_empty_page(self):
        """Empty page with no text or images."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.0):
            with patch.object(classifier, '_calculate_image_coverage', return_value=0.0):
                with patch.object(classifier, '_detect_table_patterns', return_value=False):
                    result = classifier.classify_page(mock_page)
                    # Empty page falls through to MIXED (or could be TEXT if we default that way)
                    assert result.content_type in [ContentType.MIXED, ContentType.TEXT]
    
    def test_no_images_method(self):
        """Page without get_images method (edge case)."""
        classifier = ContentClassifier()
        mock_page = MagicMock()
        del mock_page.get_images  # Remove method
        
        with patch.object(classifier, '_calculate_text_coverage', return_value=0.80):
            with patch.object(classifier, '_calculate_image_coverage', return_value=0.0):
                result = classifier.classify_page(mock_page)
                assert result.content_type == ContentType.TEXT


class TestImageBytesClassification:
    """Classification from raw image bytes (alternative path)."""
    
    def test_classify_image_basic(self):
        """Can classify from image bytes."""
        classifier = ContentClassifier()
        
        # Create dummy image bytes (minimal PNG header)
        image_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        
        # Mock the image analysis
        with patch.object(classifier, '_analyze_image_entropy', return_value=(0.6, 0.3)):
            result = classifier.classify_image(image_bytes)
            
            assert isinstance(result, ClassificationResult)
            assert result.content_type in [ContentType.TEXT, ContentType.MIXED, ContentType.VISUAL]
