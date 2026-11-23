"""Regression tests for embed-pipeline robustness fixes (#97).

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
# #97 — single-sourced extension sets
# ---------------------------------------------------------------------------

class TestFiletypesSingleSource:
    def test_consumers_derive_from_one_module(self):
        from carta.embed import filetypes, induct, lifecycle, pipeline
        from carta.scanner import scanner
        assert induct.SPREADSHEET_SUFFIXES is filetypes.SPREADSHEET_SUFFIXES
        assert set(pipeline._SUPPORTED_EXTENSIONS) == set(filetypes.EMBEDDABLE_SUFFIXES)
        assert lifecycle._BINARY_SUFFIXES == filetypes.BINARY_SUFFIXES
        assert filetypes.SPREADSHEET_SUFFIXES <= scanner._EMBED_EXTENSIONS
        assert filetypes.PDF_SUFFIXES <= scanner._EMBED_EXTENSIONS
        assert filetypes.AUDIO_SUFFIXES <= scanner._EMBED_EXTENSIONS

    def test_audit_disconnected_files_sees_spreadsheets_and_uppercase(self, tmp_path):
        from carta.audit.audit import detect_disconnected_files
        docs = tmp_path / "docs"
        docs.mkdir()
        for name in ("a.md", "b.PDF", "c.csv", "d.XLSX", "e.txt"):
            (docs / name).write_text("x")
        issues = detect_disconnected_files(tmp_path, {"docs_root": "docs"}, {}, {})
        found = {Path(i["file_path"]).name for i in issues}
        assert found == {"a.md", "b.PDF", "c.csv", "d.XLSX"}
