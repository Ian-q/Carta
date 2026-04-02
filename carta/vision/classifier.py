"""PDF page content classification for optimal vision model routing.

Analyzes PDF pages to determine content type (text/visual/mixed) and routes
to appropriate extraction model: GLM-OCR for text/tables, LLaVA for visuals.
"""
from enum import Enum
from dataclasses import dataclass
from typing import Literal, Optional, Any
import io


class ContentType(Enum):
    """Classification of page content type."""
    TEXT = "text"       # >70% text or table structures detected
    MIXED = "mixed"     # 30-70% text with visual elements  
    VISUAL = "visual"   # <30% text, image-heavy


@dataclass
class ClassificationResult:
    """Result of page content classification.
    
    Attributes:
        content_type: The classified type (TEXT, MIXED, or VISUAL)
        text_coverage: Proportion of page covered by text (0.0-1.0)
        image_coverage: Proportion of page covered by images (0.0-1.0)
        has_tables: Whether table structures were detected
        confidence: Confidence in classification (0.0-1.0)
    """
    content_type: ContentType
    text_coverage: float
    image_coverage: float
    has_tables: bool
    confidence: float


class ContentClassifier:
    """Analyzes PDF pages to determine content type for model routing.
    
    Uses heuristics on text density, image density, and table patterns
    to classify pages for optimal extraction strategy.
    
    Args:
        text_threshold: Coverage threshold for TEXT classification (default 0.70)
        visual_threshold: Image coverage threshold for VISUAL classification (default 0.40)
    """
    
    def __init__(
        self,
        text_threshold: float = 0.70,
        visual_threshold: float = 0.40
    ):
        self.text_threshold = text_threshold
        self.visual_threshold = visual_threshold
    
    def classify_page(self, page: Any) -> ClassificationResult:
        """Analyze a PyMuPDF page and return content classification.
        
        Args:
            page: PyMuPDF fitz.Page object
            
        Returns:
            ClassificationResult with content type and metrics
        """
        # Calculate coverage metrics
        text_coverage = self._calculate_text_coverage(page)
        image_coverage = self._calculate_image_coverage(page)
        has_tables = self._detect_table_patterns_from_page(page)
        
        # Determine content type based on thresholds
        content_type = self._determine_content_type(
            text_coverage, image_coverage, has_tables
        )
        
        # Calculate confidence (simplified heuristic)
        confidence = self._calculate_confidence(
            text_coverage, image_coverage, content_type
        )
        
        return ClassificationResult(
            content_type=content_type,
            text_coverage=text_coverage,
            image_coverage=image_coverage,
            has_tables=has_tables,
            confidence=confidence
        )
    
    def classify_image(self, image_bytes: bytes) -> ClassificationResult:
        """Alternative: Classify from raw image bytes.
        
        Used for pre-rendered pages or when PyMuPDF page object unavailable.
        Uses image entropy analysis as fallback heuristics.
        
        Args:
            image_bytes: Raw image data (PNG, JPEG, etc.)
            
        Returns:
            ClassificationResult (estimates only)
        """
        try:
            text_density, visual_density = self._analyze_image_entropy(image_bytes)
            
            # Apply same thresholds to entropy estimates
            has_tables = False  # Can't detect tables from image alone
            content_type = self._determine_content_type(
                text_density, visual_density, has_tables
            )
            confidence = 0.6  # Lower confidence for image-based classification
            
            return ClassificationResult(
                content_type=content_type,
                text_coverage=text_density,
                image_coverage=visual_density,
                has_tables=has_tables,
                confidence=confidence
            )
        except Exception:
            # Fallback to MIXED if image analysis fails
            return ClassificationResult(
                content_type=ContentType.MIXED,
                text_coverage=0.5,
                image_coverage=0.3,
                has_tables=False,
                confidence=0.5
            )
    
    def _calculate_text_coverage(self, page: Any) -> float:
        """Calculate proportion of page covered by text blocks.
        
        Args:
            page: PyMuPDF fitz.Page object
            
        Returns:
            Float 0.0-1.0 representing text coverage proportion
        """
        try:
            # Get text blocks with bounding boxes
            blocks = page.get_text("blocks")
            if not blocks:
                return 0.0
            
            # Calculate total text area
            text_area = 0.0
            page_rect = page.rect
            page_area = page_rect.width * page_rect.height
            
            if page_area == 0:
                return 0.0
            
            for block in blocks:
                if isinstance(block, (tuple, list)) and len(block) >= 4:
                    # Block format: (x0, y0, x1, y1, ...)
                    x0, y0, x1, y1 = block[0], block[1], block[2], block[3]
                    block_area = (x1 - x0) * (y1 - y0)
                    text_area += block_area
            
            return min(text_area / page_area, 1.0)
        except Exception:
            return 0.0
    
    def _calculate_image_coverage(self, page: Any) -> float:
        """Calculate proportion of page covered by images.
        
        Args:
            page: PyMuPDF fitz.Page object
            
        Returns:
            Float 0.0-1.0 representing image coverage proportion
        """
        try:
            images = page.get_images()
            if not images:
                return 0.0
            
            # Calculate total image area
            image_area = 0.0
            page_rect = page.rect
            page_area = page_rect.width * page_rect.height
            
            if page_area == 0:
                return 0.0
            
            for img in images:
                if isinstance(img, (tuple, list)) and len(img) >= 4:
                    # Image format: (xref, smask, width, height, ...)
                    width, height = img[2], img[3]
                    img_area = width * height
                    image_area += img_area
            
            return min(image_area / page_area, 1.0)
        except Exception:
            return 0.0
    
    def _detect_table_patterns_from_page(self, page: Any) -> bool:
        """Detect table structures from page text blocks.
        
        Analyzes text block positions to detect aligned columns
        that suggest table/tabular layout.
        
        Args:
            page: PyMuPDF fitz.Page object
            
        Returns:
            True if table patterns detected, False otherwise
        """
        try:
            # Get text blocks
            blocks = page.get_text("blocks")
            if not blocks or len(blocks) < 4:
                return False
            
            # Extract x-coordinates of text blocks
            x_positions = []
            for block in blocks:
                if isinstance(block, (tuple, list)) and len(block) >= 2:
                    x0 = block[0]
                    x_positions.append(x0)
            
            if len(x_positions) < 4:
                return False
            
            return self._detect_table_patterns(x_positions)
        except Exception:
            return False
    
    def _detect_table_patterns(self, text_blocks: list) -> bool:
        """Detect table patterns from text block x-coordinates.
        
        Looks for aligned x-positions suggesting columns.
        
        Args:
            text_blocks: List of text block dicts or x-coordinates
            
        Returns:
            True if table-like column alignment detected
        """
        try:
            # Extract x0 positions
            if not text_blocks:
                return False
            
            x_positions = []
            for block in text_blocks:
                if isinstance(block, dict):
                    if 'x0' in block:
                        x_positions.append(block['x0'])
                    elif 'bbox' in block:
                        x_positions.append(block['bbox'][0])
                elif isinstance(block, (list, tuple)):
                    if len(block) >= 1 and isinstance(block[0], (int, float)):
                        x_positions.append(block[0])
            
            if len(x_positions) < 4:
                return False
            
            # Group x-positions into clusters (columns)
            tolerance = 20  # pixels
            clusters = []
            
            for x in sorted(x_positions):
                matched = False
                for cluster in clusters:
                    if abs(x - cluster[0]) < tolerance:
                        cluster.append(x)
                        matched = True
                        break
                if not matched:
                    clusters.append([x])
            
            # Need at least 2 columns with multiple entries each
            significant_clusters = [c for c in clusters if len(c) >= 2]
            return len(significant_clusters) >= 2
            
        except Exception:
            return False
    
    def _determine_content_type(
        self,
        text_coverage: float,
        image_coverage: float,
        has_tables: bool
    ) -> ContentType:
        """Determine content type from metrics.
        
        Classification rules:
        - TEXT: >threshold text OR has_tables
        - VISUAL: <0.30 text AND >threshold images
        - MIXED: Everything else
        
        Args:
            text_coverage: Proportion of page with text
            image_coverage: Proportion of page with images
            has_tables: Whether table patterns detected
            
        Returns:
            ContentType classification
        """
        # High text coverage or tables → TEXT
        if text_coverage >= self.text_threshold or has_tables:
            return ContentType.TEXT
        
        # Low text, high images → VISUAL
        if text_coverage < 0.30 and image_coverage >= self.visual_threshold:
            return ContentType.VISUAL
        
        # Everything else → MIXED
        return ContentType.MIXED
    
    def _calculate_confidence(
        self,
        text_coverage: float,
        image_coverage: float,
        content_type: ContentType
    ) -> float:
        """Calculate confidence score for classification.
        
        Higher confidence when metrics clearly fall into one category.
        
        Args:
            text_coverage: Text coverage proportion
            image_coverage: Image coverage proportion
            content_type: Assigned content type
            
        Returns:
            Confidence score 0.0-1.0
        """
        # Distance from boundaries indicates confidence
        if content_type == ContentType.TEXT:
            # Confidence based on how far above threshold
            return min(0.95, 0.7 + (text_coverage - self.text_threshold) * 0.5)
        elif content_type == ContentType.VISUAL:
            # Confidence based on image density
            return min(0.95, 0.7 + (image_coverage - self.visual_threshold) * 0.5)
        else:  # MIXED
            # Lower confidence for mixed (by definition ambiguous)
            return 0.75
    
    def _analyze_image_entropy(self, image_bytes: bytes) -> tuple[float, float]:
        """Analyze image entropy to estimate text vs visual content.
        
        Uses simple heuristics on image data (not full computer vision).
        This is a fallback when PyMuPDF page object unavailable.
        
        Args:
            image_bytes: Raw image data
            
        Returns:
            Tuple of (text_density_estimate, visual_density_estimate)
        """
        try:
            # Try to use PIL if available
            from PIL import Image
            import numpy as np
            
            img = Image.open(io.BytesIO(image_bytes))
            
            # Convert to grayscale for analysis
            if img.mode != 'L':
                img_gray = img.convert('L')
            else:
                img_gray = img
            
            # Get pixel data
            pixels = list(img_gray.getdata())
            if not pixels:
                return (0.5, 0.5)
            
            # Calculate variance (higher variance often indicates text/structure)
            mean_val = sum(pixels) / len(pixels)
            variance = sum((p - mean_val) ** 2 for p in pixels) / len(pixels)
            normalized_variance = variance / (255 * 255)  # Normalize to 0-1
            
            # Edge detection proxy (simple gradient)
            width, height = img_gray.size
            if width > 1 and height > 1:
                img_array = np.array(img_gray)
                # Simple gradient magnitude
                dx = np.diff(img_array, axis=1)
                dy = np.diff(img_array, axis=0)
                edge_magnitude = np.mean(np.abs(dx[:, :-1]) + np.abs(dy[:-1, :]))
                normalized_edges = min(edge_magnitude / 50, 1.0)  # Normalize
            else:
                normalized_edges = 0.5
            
            # High variance + edges → likely text/structured
            # Low variance + smooth → likely photo/visual
            text_density = min(0.9, normalized_variance * 0.5 + normalized_edges * 0.5 + 0.2)
            visual_density = 1.0 - text_density
            
            return (text_density, visual_density)
            
        except ImportError:
            # PIL not available, use basic byte analysis
            return self._analyze_bytes_basic(image_bytes)
        except Exception:
            # Any error, return neutral
            return (0.5, 0.5)
    
    def _analyze_bytes_basic(self, image_bytes: bytes) -> tuple[float, float]:
        """Basic byte-level analysis when PIL unavailable.
        
        Args:
            image_bytes: Raw image data
            
        Returns:
            Tuple of (text_estimate, visual_estimate)
        """
        if len(image_bytes) < 100:
            return (0.5, 0.5)
        
        # Simple entropy calculation
        byte_counts = {}
        for byte in image_bytes[:1000]:  # Sample first 1000 bytes
            byte_counts[byte] = byte_counts.get(byte, 0) + 1
        
        # Calculate entropy
        total = len(image_bytes[:1000])
        entropy = 0.0
        for count in byte_counts.values():
            p = count / total
            if p > 0:
                entropy -= p * (p.bit_length() - 1)  # Approximation
        
        # Higher entropy often indicates more complex images
        normalized_entropy = min(entropy / 8, 1.0)
        
        # Rough heuristic: very high entropy → more visual/complex
        # Moderate entropy → text/structured
        if normalized_entropy > 0.7:
            return (0.3, 0.7)  # More visual
        else:
            return (0.6, 0.4)  # More text-like


# Convenience function for direct usage
def classify_page_content(page: Any, **kwargs) -> ClassificationResult:
    """Classify a PDF page for content type.
    
    Convenience function that creates a ContentClassifier with default
    settings and classifies the page.
    
    Args:
        page: PyMuPDF fitz.Page object
        **kwargs: Passed to ContentClassifier constructor
        
    Returns:
        ClassificationResult with content type and metrics
        
    Example:
        >>> import fitz
        >>> doc = fitz.open("datasheet.pdf")
        >>> page = doc[0]
        >>> result = classify_page_content(page)
        >>> print(result.content_type)  # ContentType.TEXT
    """
    classifier = ContentClassifier(**kwargs)
    return classifier.classify_page(page)
