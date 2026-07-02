"""Integration tests: spreadsheet sources through the embed pipeline."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CFG = {"project_name": "p", "qdrant_url": "http://localhost:6333", "embed": {}}

CSV_MIXED = (
    "MsgID,Signal,StartBit,Notes\n"
    "0x100,BMS_PackVoltage,0,\n"
    "0x2B0,TMS_CoolantTemp,16,only valid when pump enabled\n"
)

CSV_NUMERIC_ONLY = "MsgID,StartBit\n0x100,0\n0x200,8\n"


def _mk_docs(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    return docs


def _embed(tmp_path, rel):
    from carta.embed.pipeline import _embed_one_file
    fp = tmp_path / rel
    info = {"slug": fp.stem, "doc_type": "unknown", "generation": 1}
    calls = {}

    def fake_upsert(enriched, cfg, client=None):
        calls["chunks"] = enriched
        return len([c for c in enriched if (c.get("text") or "").strip()])

    with patch("carta.embed.pipeline.upsert_chunks", side_effect=fake_upsert), \
         patch("carta.embed.pipeline.delete_other_points"):
        count, updates = _embed_one_file(fp, info, CFG, MagicMock(), tmp_path, 400, 0.15)
    return count, updates, calls


class TestSpreadsheetDispatch:
    def test_csv_embeds_with_workbook_citation_and_companion(self, tmp_path):
        docs = _mk_docs(tmp_path)
        (docs / "battery.csv").write_text(CSV_MIXED)
        count, updates, calls = _embed(tmp_path, "docs/battery.csv")
        assert count > 0
        assert updates["status"] == "embedded"
        chunk = calls["chunks"][0]
        # citations resolve to the WORKBOOK, not the companion
        assert chunk["file_path"] == "docs/battery.csv"
        assert chunk["derived"] == "spreadsheet"
        assert chunk["companion_path"] == ".carta/companions/docs/battery.csv.md"
        companion = tmp_path / ".carta" / "companions" / "docs" / "battery.csv.md"
        assert companion.exists()
        assert "TMS_CoolantTemp" in companion.read_text()

    def test_numeric_only_csv_flags_no_text_content(self, tmp_path):
        docs = _mk_docs(tmp_path)
        (docs / "ids.csv").write_text(CSV_NUMERIC_ONLY)
        count, updates, calls = _embed(tmp_path, "docs/ids.csv")
        assert count == 0
        assert updates["status"] == "no_text_content"
        assert "chunks" not in calls  # upsert never attempted
        # transparency stub still written
        companion = tmp_path / ".carta" / "companions" / "docs" / "ids.csv.md"
        assert companion.exists()

    def test_empty_md_still_extraction_failed(self, tmp_path):
        docs = _mk_docs(tmp_path)
        (docs / "empty.md").write_text("")
        count, updates, calls = _embed(tmp_path, "docs/empty.md")
        assert count == 0
        assert updates["status"] == "extraction_failed"

    def test_uppercase_md_routes_to_markdown_extractor(self, tmp_path):
        docs = _mk_docs(tmp_path)
        (docs / "NOTES.MD").write_text("# Title\n\nSome body text here.\n")
        count, updates, calls = _embed(tmp_path, "docs/NOTES.MD")
        assert count > 0
        assert updates["status"] == "embedded"
        assert any("Some body text" in c["text"] for c in calls["chunks"])

    def test_missing_openpyxl_leaves_sidecar_pending(self, tmp_path, capsys):
        docs = _mk_docs(tmp_path)
        (docs / "wb.xlsx").write_bytes(b"PK\x03\x04")
        with patch.dict(sys.modules, {"openpyxl": None}):
            count, updates, calls = _embed(tmp_path, "docs/wb.xlsx")
        assert count == 0
        assert updates["status"] == "pending"  # re-pickable once installed
        assert "openpyxl is not installed" in capsys.readouterr().err


class TestSpreadsheetDiscovery:
    def test_iter_inductable_includes_spreadsheets_excludes_carta(self, tmp_path):
        from carta.embed.pipeline import _iter_inductable_files
        docs = _mk_docs(tmp_path)
        (docs / "a.csv").write_text("x\n")
        (docs / "b.XLSX").write_bytes(b"PK")
        comp = tmp_path / ".carta" / "companions" / "docs"
        comp.mkdir(parents=True)
        (comp / "a.csv.md").write_text("derived artifact")
        # docs_root="." worst case: sweep the repo root itself
        found = {p.name for p in _iter_inductable_files(tmp_path)}
        assert {"a.csv", "b.XLSX"} <= found
        assert "a.csv.md" not in found
