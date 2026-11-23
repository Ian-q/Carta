"""Regression tests for embed-pipeline robustness fixes (#98).

Every fixture here is invented; nothing is copied from a real project.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

CFG = {
    "project_name": "p",
    "qdrant_url": "http://localhost:6333",
    "embed": {"ollama_url": "http://x", "ollama_model": "m"},
}


def _setup_repo(tmp_path: Path) -> Path:
    cfg_path = tmp_path / ".carta" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.touch()
    return cfg_path


def _write_sidecar(tmp_path: Path, rel: str, data: dict) -> Path:
    from carta.embed.induct import sidecar_path
    sc = sidecar_path(tmp_path / rel, tmp_path)
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text(yaml.safe_dump(data))
    return sc


# ---------------------------------------------------------------------------
# #98 — uppercase .PDF vision gates
# ---------------------------------------------------------------------------

def test_uppercase_pdf_queues_image_heavy_pages(tmp_path):
    from carta.embed.pipeline import _embed_one_file
    from carta.embed.visual_queue import VISUAL_PENDING_KEY
    from carta.vision.classifier import PageClass

    pdf = tmp_path / "docs" / "WIDGET.PDF"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    pages = [
        {"page": 1, "text": "plain words about the widget " * 10, "headings": []},
        {"page": 2, "text": "caption for a figure " * 10, "headings": []},
    ]
    classes = [PageClass.PURE_TEXT, PageClass.TEXT_WITH_IMAGES]
    info = {"slug": "WIDGET", "doc_type": "unknown", "generation": 1}
    with patch("carta.embed.pipeline.PageAnalyzer"), \
         patch("carta.embed.pipeline.extract_pdf_text_and_classify",
               return_value=(pages, classes)), \
         patch("carta.embed.pipeline.upsert_chunks",
               side_effect=lambda ch, cfg, client=None: len(ch)), \
         patch("carta.embed.pipeline.delete_other_points"):
        _count, updates = _embed_one_file(pdf, info, CFG, MagicMock(), tmp_path, 400, 0.15)
    assert updates.get(VISUAL_PENDING_KEY) == [2]


def test_uppercase_pdf_zero_text_is_ocr_recoverable(tmp_path):
    from carta.embed.pipeline import _embed_one_file
    from carta.embed.visual_queue import VISUAL_PENDING_KEY
    from carta.vision.classifier import PageClass

    pdf = tmp_path / "docs" / "SCAN.PDF"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    pages = [{"page": 1, "text": "", "headings": []}, {"page": 2, "text": "", "headings": []}]
    info = {"slug": "SCAN", "doc_type": "unknown", "generation": 1}
    with patch("carta.embed.pipeline.PageAnalyzer"), \
         patch("carta.embed.pipeline.extract_pdf_text_and_classify",
               return_value=(pages, [PageClass.PURE_TEXT, PageClass.PURE_TEXT])), \
         patch("carta.embed.pipeline.upsert_chunks", return_value=0), \
         patch("carta.embed.pipeline.delete_other_points"):
        _count, updates = _embed_one_file(pdf, info, CFG, MagicMock(), tmp_path, 400, 0.15)
    assert updates["status"] != "extraction_failed"
    assert updates.get(VISUAL_PENDING_KEY) == [1, 2]
