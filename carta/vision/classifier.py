"""PDF page classification for smart vision routing.

Analyzes PyMuPDF page objects using free metadata (zero model calls) to
determine the appropriate extraction strategy for each page.
"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


FIGURE_CAPTION_RE = re.compile(
    r'\b(fig\.?|figure|plot|chart|diagram|graph)\s*\d+\b',
    re.IGNORECASE,
)


class PageClass(Enum):
    """Routing class for a PDF page."""
    PURE_TEXT = "pure_text"
    STRUCTURED_TEXT = "structured_text"
    TEXT_WITH_IMAGES = "text_with_images"
    FLATTENED = "flattened"


@dataclass
class PageProfile:
    """Signals extracted from a PDF page for routing decisions.

    Attributes:
        text_length:  Characters returned by page.get_text()
        has_images:   True if page.get_images() is non-empty
        has_tables:   True if column-alignment heuristic fires
        has_captions: True if FIGURE_CAPTION_RE matches page text
        page_class:   Resulting routing class
    """
    text_length: int
    has_images: bool
    has_tables: bool
    has_captions: bool
    page_class: PageClass


class PageAnalyzer:
    """Classify a PDF page using only PyMuPDF metadata (zero model calls).

    Args:
        cfg: Carta config dict. Reads embed.vision_text_min_chars and
             embed.vision_text_max_chars.
    """

    def __init__(self, cfg: dict):
        embed = cfg.get("embed", {})
        self.text_min: int = embed.get("vision_text_min_chars", 150)
        self.text_max: int = embed.get("vision_text_max_chars", 600)

    def analyze(self, page: Any) -> PageProfile:
        """Return a PageProfile for *page*.

        Args:
            page: PyMuPDF fitz.Page object.

        Returns:
            PageProfile with page_class set.
        """
        text = page.get_text().strip()
        text_length = len(text)
        has_images = bool(page.get_images())
        has_tables = self._detect_tables(page)
        has_captions = bool(FIGURE_CAPTION_RE.search(text))
        page_class = self._classify(text_length, has_images, has_tables, has_captions)
        return PageProfile(
            text_length=text_length,
            has_images=has_images,
            has_tables=has_tables,
            has_captions=has_captions,
            page_class=page_class,
        )

    def _classify(
        self,
        text_length: int,
        has_images: bool,
        has_tables: bool,
        has_captions: bool,
    ) -> PageClass:
        if text_length < self.text_min:
            return PageClass.FLATTENED
        if has_tables:
            return PageClass.STRUCTURED_TEXT
        if has_images or (has_captions and text_length < self.text_max):
            return PageClass.TEXT_WITH_IMAGES
        return PageClass.PURE_TEXT

    def _detect_tables(self, page: Any) -> bool:
        """Detect table structures via column-alignment heuristic."""
        try:
            blocks = page.get_text("blocks")
            if not blocks or len(blocks) < 4:
                return False
            x_positions = [
                b[0] for b in blocks
                if isinstance(b, (tuple, list)) and len(b) >= 1
                and isinstance(b[0], (int, float))
            ]
            if len(x_positions) < 4:
                return False
            return self._has_column_alignment(x_positions)
        except Exception:
            return False

    def _has_column_alignment(self, x_positions: list[float]) -> bool:
        """Return True if x-positions cluster into ≥2 columns with ≥2 blocks each."""
        tolerance = 20
        clusters: list[list[float]] = []
        for x in sorted(x_positions):
            for cluster in clusters:
                if abs(x - cluster[0]) < tolerance:
                    cluster.append(x)
                    break
            else:
                clusters.append([x])
        return len([c for c in clusters if len(c) >= 2]) >= 2
