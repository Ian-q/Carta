"""Dual extraction router for intelligent PDF vision processing.

Routes PDF pages to optimal vision model based on content classification:
- GLM-OCR for text-heavy pages (datasheets, tables)
- LLaVA for visual pages (plots, schematics)
- Hybrid for mixed content (both models, combined)
"""
import base64
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
import requests

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore

from carta.vision.classifier import ContentClassifier, ContentType, classify_page_content


@dataclass
class ExtractionResult:
    """Result from page content extraction.
    
    Attributes:
        text: Extracted text content
        model_used: Which model performed extraction (glm-ocr, llava, hybrid)
        content_type: Classification of page content
        page_num: Page number in PDF
        confidence: Confidence in extraction quality
        has_tables: Whether tables were detected
    """
    text: str
    model_used: Literal["glm-ocr", "llava", "hybrid"]
    content_type: str
    page_num: int
    confidence: float
    has_tables: bool = False


class DualExtractionRouter:
    """Routes PDF pages to appropriate vision model based on content classification.
    
    This class integrates the ContentClassifier with the extraction pipeline,
    choosing between GLM-OCR and LLaVA based on page content analysis.
    
    Args:
        cfg: Carta config dict with embed.ocr_model, embed.ollama_vision_model,
             and optional embed.classification thresholds
    """
    
    GLM_OCR_PROMPT = """Extract all text content from this document page.

If tables are present, format them as markdown tables with proper headers and alignment.
Preserve numerical values, units, and specifications exactly as shown in the original.
Maintain the document's hierarchical structure (headers, lists, paragraphs).

For technical specifications:
- Keep all numbers, units, and tolerances intact
- Preserve register addresses and bit field descriptions
- Maintain pin numbers and signal names exactly

Output only the extracted content with markdown formatting. No explanatory text."""

    LLAVA_PROMPT = (
        "Describe this technical diagram for engineering documentation search. "
        "Include: data values, axis labels, register names, waveform descriptions, "
        "block labels, pin names, and any visible technical annotations."
    )
    
    def __init__(self, cfg: dict):
        self.cfg = cfg
        embed_cfg = cfg.get("embed", {})
        
        # Model names from config
        self.ocr_model = embed_cfg.get("ocr_model", "glm-ocr:latest")
        self.vision_model = embed_cfg.get("ollama_vision_model", "llava:latest")
        
        # Classification thresholds
        classification_cfg = embed_cfg.get("classification", {})
        text_threshold = classification_cfg.get("text_threshold", 0.70)
        visual_threshold = classification_cfg.get("visual_threshold", 0.40)
        
        # Routing strategy
        self.vision_routing = embed_cfg.get("vision_routing", "auto")
        
        # Initialize classifier
        self.classifier = ContentClassifier(
            text_threshold=text_threshold,
            visual_threshold=visual_threshold
        )
        
        # Ollama connection
        self.ollama_url = embed_cfg.get("ollama_url", "http://localhost:11434")
    
    def extract_page(
        self,
        page: Any,
        page_num: int,
        force_mode: Optional[Literal["ocr", "vision", "both"]] = None
    ) -> ExtractionResult:
        """Extract content from a PDF page using optimal model.
        
        Args:
            page: PyMuPDF fitz.Page object
            page_num: Page number for tracking
            force_mode: Override auto-routing (ocr, vision, both, or None for auto)
            
        Returns:
            ExtractionResult with extracted text and metadata
        """
        # Determine routing mode
        routing_mode = force_mode or self.vision_routing
        
        if routing_mode == "auto":
            # Classify page to determine optimal model
            classification = self.classifier.classify_page(page)
            content_type = classification.content_type
        else:
            # Use forced mode, set classification accordingly
            if routing_mode == "ocr":
                content_type = ContentType.TEXT
                classification = None
            elif routing_mode == "vision":
                content_type = ContentType.VISUAL
                classification = None
            else:  # both
                content_type = ContentType.MIXED
                classification = None
        
        # Route to appropriate extraction method
        if content_type == ContentType.TEXT:
            return self._extract_with_ocr(page, page_num, classification)
        elif content_type == ContentType.VISUAL:
            return self._extract_with_vision(page, page_num, classification)
        else:  # MIXED
            return self._extract_hybrid(page, page_num, classification)
    
    def _extract_with_ocr(
        self,
        page: Any,
        page_num: int,
        classification: Optional[Any] = None
    ) -> ExtractionResult:
        """Extract page content using GLM-OCR.
        
        Optimized for text-heavy pages with tables and structured data.
        """
        try:
            # Render page to image
            pix = page.get_pixmap(dpi=150)
            png_bytes = pix.tobytes("png")
            
            # Call GLM-OCR
            extracted_text = self._call_ollama_vision(
                png_bytes,
                model=self.ocr_model,
                prompt=self.GLM_OCR_PROMPT,
                timeout=60
            )
            
            has_tables = classification.has_tables if classification else False
            confidence = classification.confidence if classification else 0.85
            
            return ExtractionResult(
                text=extracted_text,
                model_used="glm-ocr",
                content_type="text",
                page_num=page_num,
                confidence=confidence,
                has_tables=has_tables
            )
            
        except Exception as exc:
            print(
                f"Warning: GLM-OCR failed for page {page_num}, falling back to LLaVA: {exc}",
                file=sys.stderr,
                flush=True
            )
            # Fallback to LLaVA
            return self._extract_with_vision(page, page_num, classification)
    
    def _extract_with_vision(
        self,
        page: Any,
        page_num: int,
        classification: Optional[Any] = None
    ) -> ExtractionResult:
        """Extract page content using LLaVA vision model.
        
        Optimized for visual content like plots, schematics, and diagrams.
        """
        try:
            # Render page to image
            pix = page.get_pixmap(dpi=150)
            png_bytes = pix.tobytes("png")
            
            # Call LLaVA
            description = self._call_ollama_vision(
                png_bytes,
                model=self.vision_model,
                prompt=self.LLAVA_PROMPT,
                timeout=60
            )
            
            confidence = classification.confidence if classification else 0.85
            
            return ExtractionResult(
                text=description,
                model_used="llava",
                content_type="visual",
                page_num=page_num,
                confidence=confidence,
                has_tables=False
            )
            
        except Exception as exc:
            print(
                f"Warning: Vision model failed for page {page_num}: {exc}",
                file=sys.stderr,
                flush=True
            )
            # Return empty result on total failure
            return ExtractionResult(
                text=f"[Extraction failed for page {page_num}: {exc}]",
                model_used="llava",
                content_type="visual",
                page_num=page_num,
                confidence=0.0,
                has_tables=False
            )
    
    def _extract_hybrid(
        self,
        page: Any,
        page_num: int,
        classification: Optional[Any] = None
    ) -> ExtractionResult:
        """Extract page content using both models and combine results.
        
        For mixed pages: GLM-OCR for text content, LLaVA for visual context.
        Combines both extractions for comprehensive coverage.
        """
        try:
            # Render page once
            pix = page.get_pixmap(dpi=150)
            png_bytes = pix.tobytes("png")
            
            # Call both models
            ocr_text = None
            vision_text = None
            
            # GLM-OCR for text content
            try:
                ocr_text = self._call_ollama_vision(
                    png_bytes,
                    model=self.ocr_model,
                    prompt=self.GLM_OCR_PROMPT,
                    timeout=60
                )
            except Exception as ocr_exc:
                print(
                    f"Warning: GLM-OCR failed for mixed page {page_num}: {ocr_exc}",
                    file=sys.stderr,
                    flush=True
                )
            
            # LLaVA for visual context
            try:
                vision_text = self._call_ollama_vision(
                    png_bytes,
                    model=self.vision_model,
                    prompt=self.LLAVA_PROMPT,
                    timeout=60
                )
            except Exception as vision_exc:
                print(
                    f"Warning: LLaVA failed for mixed page {page_num}: {vision_exc}",
                    file=sys.stderr,
                    flush=True
                )
            
            # Combine results
            if ocr_text and vision_text:
                combined_text = f"""{ocr_text}

[Visual Context]: {vision_text}"""
                model_used = "hybrid"
                confidence = 0.90
            elif ocr_text:
                combined_text = ocr_text
                model_used = "glm-ocr"
                confidence = 0.75
            elif vision_text:
                combined_text = vision_text
                model_used = "llava"
                confidence = 0.75
            else:
                combined_text = f"[Extraction failed for mixed page {page_num}]"
                model_used = "hybrid"
                confidence = 0.0
            
            has_tables = classification.has_tables if classification else False
            
            return ExtractionResult(
                text=combined_text,
                model_used=model_used,  # type: ignore
                content_type="mixed",
                page_num=page_num,
                confidence=confidence,
                has_tables=has_tables
            )
            
        except Exception as exc:
            print(
                f"Warning: Hybrid extraction failed for page {page_num}: {exc}",
                file=sys.stderr,
                flush=True
            )
            return ExtractionResult(
                text=f"[Extraction failed for page {page_num}: {exc}]",
                model_used="hybrid",  # type: ignore
                content_type="mixed",
                page_num=page_num,
                confidence=0.0,
                has_tables=False
            )
    
    def _call_ollama_vision(
        self,
        image_png_bytes: bytes,
        model: str,
        prompt: str,
        timeout: int = 60
    ) -> str:
        """Call Ollama vision model with image.
        
        Args:
            image_png_bytes: PNG image bytes
            model: Model name (glm-ocr:latest or llava:latest)
            prompt: Prompt text for the model
            timeout: Request timeout in seconds
            
        Returns:
            Model response text
            
        Raises:
            RuntimeError: If Ollama returns non-200 status
        """
        b64 = base64.b64encode(image_png_bytes).decode("utf-8")
        
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
            },
            timeout=timeout,
        )
        
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ollama vision model returned {resp.status_code}: {resp.text[:200]}"
            )
        
        return resp.json()["response"].strip()
    
    def extract_pdf(
        self,
        pdf_path: Path,
        progress_callback: Optional[Any] = None
    ) -> list[ExtractionResult]:
        """Extract content from all pages of a PDF.
        
        Args:
            pdf_path: Path to PDF file
            progress_callback: Optional callback(page_num, total_pages)
            
        Returns:
            List of ExtractionResult for each page
        """
        if fitz is None:
            raise RuntimeError("PyMuPDF (fitz) not available")
        
        try:
            doc = fitz.open(str(pdf_path))
        except Exception as exc:
            print(
                f"Warning: could not open PDF {pdf_path}: {exc}",
                file=sys.stderr,
                flush=True
            )
            return []
        
        results = []
        total_pages = len(doc)
        
        for page_num, page in enumerate(doc, start=1):
            # Call progress callback if provided
            if progress_callback:
                try:
                    progress_callback(page_num, total_pages)
                except Exception:
                    pass  # Callback errors shouldn't stop extraction
            
            # Extract page content
            result = self.extract_page(page, page_num)
            results.append(result)
        
        doc.close()
        return results


# Convenience function for direct usage
def extract_pdf_with_intelligent_routing(
    pdf_path: Path,
    cfg: dict,
    progress_callback: Optional[Any] = None
) -> list[ExtractionResult]:
    """Extract PDF content with intelligent model routing.
    
    Convenience function that creates a DualExtractionRouter and extracts
    all pages from a PDF.
    
    Args:
        pdf_path: Path to PDF file
        cfg: Carta config dict
        progress_callback: Optional callback(page_num, total_pages)
        
    Returns:
        List of ExtractionResult for each page
        
    Example:
        >>> from carta.vision.router import extract_pdf_with_intelligent_routing
        >>> results = extract_pdf_with_intelligent_routing(
        ...     Path("datasheet.pdf"),
        ...     cfg
        ... )
        >>> for r in results:
        ...     print(f"Page {r.page_num}: {r.model_used} - {r.content_type}")
    """
    router = DualExtractionRouter(cfg)
    return router.extract_pdf(pdf_path, progress_callback)


# Backward compatibility: provide extract_image_descriptions interface
def extract_image_descriptions_intelligent(
    pdf_path: Path,
    cfg: dict,
    progress_callback: Optional[Any] = None
) -> list[dict]:
    """Extract image descriptions from PDF using intelligent model routing.
    
    This is a drop-in replacement for carta.embed.vision.extract_image_descriptions
    that uses DualExtractionRouter for intelligent model selection.
    
    Compatible return format for existing pipeline integration:
    - doc_type: "image_description"
    - page_num: page number
    - image_index: always 0 (page-level extraction)
    - text: extracted content
    - model_used: which model extracted (glm-ocr, llava, hybrid)
    - content_type: text/visual/mixed
    
    Args:
        pdf_path: path to PDF file
        cfg: carta config dict (requires embed.ollama_url, embed.ocr_model, etc.)
        progress_callback: optional callback(page_num, total_pages)
        
    Returns:
        List of dicts compatible with extract_image_descriptions format,
        with additional keys for extraction metadata.
    """
    results = extract_pdf_with_intelligent_routing(pdf_path, cfg, progress_callback)
    
    # Convert to legacy format with extra metadata
    legacy_results = []
    for result in results:
        legacy_results.append({
            "doc_type": "image_description",
            "page_num": result.page_num,
            "image_index": 0,  # Page-level extraction
            "text": result.text,
            # Additional metadata for new sidecar fields
            "model_used": result.model_used,
            "content_type": result.content_type,
            "confidence": result.confidence,
            "has_tables": result.has_tables,
        })
    
    return legacy_results
