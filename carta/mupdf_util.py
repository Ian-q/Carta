"""Control MuPDF console noise from PyMuPDF.

Vendor PDFs sometimes ship with broken tagged/structure trees. MuPDF can still
extract text and render pages, but logs ``format error: No common ancestor in
structure tree`` (and similar) to stderr. Carta suppresses that during PDF work.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


@contextmanager
def mupdf_quiet() -> Iterator[None]:
    """Disable MuPDF error and warning lines on stderr for the enclosed block.

    Restores default display flags afterward. No-op if PyMuPDF is not installed.
    """
    try:
        import fitz
    except ImportError:
        yield
        return

    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    try:
        yield
    finally:
        fitz.TOOLS.mupdf_display_errors(True)
        fitz.TOOLS.mupdf_display_warnings(True)
