"""Tests for carta.embed.tabular — deterministic spreadsheet extraction."""

from pathlib import Path

import pytest

from carta.embed.tabular import (
    extract_spreadsheet_text,
    companion_rel_path,
    write_companion,
)

# A CAN-dictionary-shaped fixture: hex numeric IDs, text signal names, numeric
# start bits, and a notes column. 0x1A3 and start bit 8 are mid-range values
# that must NOT survive extraction (only range endpoints appear).
CSV_MIXED = (
    "MsgID,Signal,StartBit,Notes\n"
    "0x100,BMS_PackVoltage,0,\n"
    "0x1A3,BMS_PackCurrent,8,\n"
    "0x2B0,TMS_CoolantTemp,16,only valid when pump enabled (see rows 40-60)\n"
    "0x7FF,TMS_FlowRate,24,\n"
)

CSV_NUMERIC_ONLY = (
    "MsgID,StartBit\n"
    "0x100,0\n"
    "0x200,8\n"
)


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return p


class TestCsvExtraction:
    def test_mixed_sheet_keeps_text_drops_numeric_cells(self, tmp_path):
        p = _write(tmp_path, "battery.csv", CSV_MIXED)
        pages, meta = extract_spreadsheet_text(p)
        assert len(pages) == 1
        text = pages[0]["text"]
        # BM25 targets present
        for sig in ("BMS_PackVoltage", "BMS_PackCurrent", "TMS_CoolantTemp", "TMS_FlowRate"):
            assert sig in text
        # notes preserved verbatim
        assert "only valid when pump enabled (see rows 40-60)" in text
        # mid-range numeric cell values absent; range endpoints present
        assert "0x1A3" not in text
        assert "0x100" in text and "0x7FF" in text
        # numeric column renders as header + range only
        assert "StartBit (numeric 0-24)" in text or "StartBit (numeric 0–24)" in text

    def test_notes_bullets_keyed_by_first_text_column(self, tmp_path):
        p = _write(tmp_path, "battery.csv", CSV_MIXED)
        pages, _ = extract_spreadsheet_text(p)
        assert "- TMS_CoolantTemp: only valid when pump enabled" in pages[0]["text"]

    def test_csv_is_single_page_with_stem_heading(self, tmp_path):
        p = _write(tmp_path, "battery.csv", CSV_MIXED)
        pages, meta = extract_spreadsheet_text(p)
        assert pages[0]["page"] == 1
        assert pages[0]["headings"] == ["battery"]
        assert meta["sheet_names"] == ["battery"]

    def test_numeric_only_sheet_yields_empty_page_but_companion_stub(self, tmp_path):
        p = _write(tmp_path, "ids.csv", CSV_NUMERIC_ONLY)
        pages, meta = extract_spreadsheet_text(p)
        assert pages[0]["text"] == ""
        # transparency stub still rendered in the companion
        assert "Columns:" in meta["companion_markdown"]
        assert "MsgID (numeric 0x100" in meta["companion_markdown"]

    def test_text_values_deduplicated(self, tmp_path):
        p = _write(tmp_path, "dup.csv", "Signal\nA_Sig\nA_Sig\nB_Sig\n")
        pages, _ = extract_spreadsheet_text(p)
        assert pages[0]["text"].count("A_Sig") == 1

    def test_long_free_text_cell_promotes_column_to_notes(self, tmp_path):
        long = "x" * 80
        p = _write(tmp_path, "f.csv", f"Signal,Extra\nA_Sig,{long}\nB_Sig,\n")
        pages, _ = extract_spreadsheet_text(p)
        assert long in pages[0]["text"]        # full text kept (notes semantics)
        assert "## Extra" in pages[0]["text"]

    def test_long_text_in_numeric_dominant_column_still_promoted_to_notes(self, tmp_path):
        # Extra column values = [long, 7, 8, 9, 10] -> 4/5 = 80% numeric, which
        # meets NUMERIC_THRESHOLD; the free-text promotion must still win so the
        # long "gotcha" cell is not silently dropped.
        long = "y" * 80
        p = _write(tmp_path, "outlier.csv",
                   f"ID,Extra\n1,{long}\n2,7\n3,8\n4,9\n5,10\n")
        pages, _ = extract_spreadsheet_text(p)
        assert long in pages[0]["text"]

    def test_mixed_column_below_threshold_drops_numeric_strays(self, tmp_path):
        p = _write(tmp_path, "mixed.csv", "Code\n1\n2\nA_Sig\nB_Sig\nC_Sig\n")
        pages, _ = extract_spreadsheet_text(p)
        text = pages[0]["text"]
        assert "A_Sig" in text and "B_Sig" in text and "C_Sig" in text
        assert "1, 2" not in text and "## Code\n1" not in text

    def test_numeric_stray_key_falls_back_to_row_number(self, tmp_path):
        p = _write(tmp_path, "straykey.csv",
                   "Code,Notes\n1,gotcha applies here\nA_Sig,\nB_Sig,\n")
        pages, _ = extract_spreadsheet_text(p)
        text = pages[0]["text"]
        assert "- row 2: gotcha applies here" in text
        assert "- 1: gotcha applies here" not in text

    def test_hex_column_summarized_as_hex_range(self, tmp_path):
        p = _write(tmp_path, "h.csv", "ID,Name\n0x100,A_Sig\n0x7FF,B_Sig\n")
        pages, _ = extract_spreadsheet_text(p)
        assert "0x100" in pages[0]["text"] and "0x7FF" in pages[0]["text"]

    def test_companion_marked_auto_generated(self, tmp_path):
        p = _write(tmp_path, "battery.csv", CSV_MIXED)
        _, meta = extract_spreadsheet_text(p)
        assert "auto-generated by carta" in meta["companion_markdown"]


class TestCompanionHelpers:
    def test_companion_rel_path_appends_md(self):
        assert companion_rel_path(Path("docs/battery.xlsx")) == (
            Path(".carta") / "companions" / "docs" / "battery.xlsx.md")

    def test_companion_paths_do_not_collide_across_types(self):
        assert companion_rel_path(Path("docs/data.csv")) != companion_rel_path(
            Path("docs/data.xlsx"))

    def test_write_companion_creates_dirs_and_writes(self, tmp_path):
        out = write_companion(tmp_path, Path("docs/b.csv"), "content")
        assert out == tmp_path / ".carta" / "companions" / "docs" / "b.csv.md"
        assert out.read_text() == "content"

    def test_write_companion_fail_open(self, tmp_path, capsys):
        # target parent is a FILE -> mkdir raises OSError -> returns None, warns
        blocker = tmp_path / ".carta"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a dir")
        out = write_companion(tmp_path, Path("docs/b.csv"), "content")
        assert out is None
        assert "could not write companion" in capsys.readouterr().err


class TestXlsxExtraction:
    def _workbook(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "CAN_Signals"
        ws1.append(["MsgID", "Signal", "Notes"])
        ws1.append(["0x100", "BMS_PackVoltage", None])
        ws1.append(["0x2B0", "TMS_CoolantTemp", "only valid when pump enabled"])
        ws2 = wb.create_sheet("Calibration")
        ws2.append(["Param", "Value"])
        ws2.append(["GainFactor", 42])
        p = tmp_path / "battery.xlsx"
        wb.save(p)
        return p

    def test_one_page_per_sheet_with_sheet_headings(self, tmp_path):
        p = self._workbook(tmp_path)
        pages, meta = extract_spreadsheet_text(p)
        assert [pg["page"] for pg in pages] == [1, 2]
        assert pages[0]["headings"] == ["CAN_Signals"]
        assert pages[1]["headings"] == ["Calibration"]
        assert meta["sheet_names"] == ["CAN_Signals", "Calibration"]

    def test_xlsx_text_and_notes_extracted(self, tmp_path):
        p = self._workbook(tmp_path)
        pages, _ = extract_spreadsheet_text(p)
        assert "TMS_CoolantTemp" in pages[0]["text"]
        assert "only valid when pump enabled" in pages[0]["text"]

    def test_missing_openpyxl_raises_actionable_error(self, tmp_path):
        import sys
        from unittest.mock import patch
        from carta.embed.tabular import OpenpyxlMissing
        p = tmp_path / "wb.xlsx"
        p.write_bytes(b"PK\x03\x04")
        with patch.dict(sys.modules, {"openpyxl": None}):
            with pytest.raises(OpenpyxlMissing, match="openpyxl is not installed"):
                extract_spreadsheet_text(p)
