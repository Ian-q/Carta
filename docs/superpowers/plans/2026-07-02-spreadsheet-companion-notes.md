# Spreadsheet Companion Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the text-bearing content of `.xlsx`/`.csv` files searchable through Carta's existing hybrid index, with a deterministic extractor, workbook-cited chunks, a regenerable companion note under `.carta/companions/`, and a `remember --about` file association.

**Architecture:** The spreadsheet itself becomes a tracked source (hashed, sidecar'd, generation-bumped); a new pure module `carta/embed/tabular.py` extracts only text columns and notes cells per sheet, returning the standard `pages` contract so chunking/embedding/citation ride the existing pipeline. Two prerequisite correctness fixes land first: raw-byte hashing for binary suffixes and extension-preserving sidecar names for the new types (prevents `data.csv`/`data.md` silently sharing one sidecar). A new terminal sidecar status `no_text_content` is registered across every status consumer.

**Tech Stack:** Python 3.10+, stdlib `csv`, `openpyxl` (optional extra, lazy import), pytest. No new infra.

**Spec:** `docs/superpowers/specs/2026-07-02-spreadsheet-companion-notes-design.md` (audited 2026-07-02 — read it before starting any task).

## Global Constraints

- Python 3.10+; `.csv` via stdlib `csv`; `.xlsx` via `openpyxl>=3.1` **only** as an optional extra with lazy import — the core `dependencies` list in `pyproject.toml` must not change.
- Zero regression to existing `.pdf`/`.md` state: their hash digests and sidecar filenames must be byte-identical before/after (pinned by tests in Tasks 1–2).
- Ordering: `_SUPPORTED_EXTENSIONS` gains `.csv`/`.xlsx` only in Task 6, strictly after Task 2 (induct `file_type` + sidecar naming) — auto-induction writes stubs for every discovered file, so flipping discovery first would write wrong (`file_type: pdf`), collision-prone stubs.
- Numeric cell values never enter embedded text — only header names + range summaries.
- `no_text_content` is terminal: not re-picked by `discover_pending_files`, not re-embedded by `--repair`, registered in every status bucket (Task 5).
- All suffix comparisons added by this plan use `.suffix.lower()`.
- Run `python3 -m pytest -q` from the repo root; the full suite must be green before every commit.
- All line numbers below are per `origin/main` @ `ee04cc4` — if drift, anchor by the quoted code.

---

### Task 0: Commit spec, sync branch with origin/main

The feature branch `feat/spreadsheet-companion-notes` was cut before PRs #90/#92/#94 merged; `carta/status.py` and `carta/embed/pipeline.py` on main differ from the branch. All later tasks target main's code.

**Files:**
- Modify: none (git only)

- [ ] **Step 1: Commit the audited spec (it has uncommitted amendments)**

```bash
cd /Users/ian/dev/doc-audit-cc
git status --short   # expect: M docs/superpowers/specs/2026-07-02-spreadsheet-companion-notes-design.md
git add docs/superpowers/specs/2026-07-02-spreadsheet-companion-notes-design.md
git commit -m "docs(spec): audit amendments — sidecar collision, status ripple, scanner row"
```

- [ ] **Step 2: Merge origin/main**

```bash
git fetch origin
git merge origin/main --no-edit
```

Expected: clean merge (the branch only adds spec/plan files). If conflicts appear, they are in docs only — resolve keeping both sides.

- [ ] **Step 3: Verify the suite is green on the merged base**

Run: `python3 -m pytest -q`
Expected: all tests pass (≈1150), 0 failures.

- [ ] **Step 4: Verify the merged base has main's status vocabulary**

Run: `grep -n "_DONE_STATUSES" carta/status.py`
Expected: `_DONE_STATUSES = ("embedded", "done")` present. If absent, STOP — the merge did not take.

---

### Task 1: Binary-suffix hashing in `compute_file_hash`

`compute_file_hash` (`carta/embed/lifecycle.py:20`) CRLF→LF-normalizes every non-`.pdf` file. A binary `.xlsx` (zip container) must hash raw: two distinct workbooks differing only in `\r\n`/`\n` bytes would otherwise collide. `.csv` is text and keeps normalization. Existing `.md`/`.pdf` digests must not change.

**Files:**
- Modify: `carta/embed/lifecycle.py:20-47`
- Test: `carta/tests/test_lifecycle.py` (class `TestComputeFileHash`)

**Interfaces:**
- Consumes: nothing new.
- Produces: unchanged signature `compute_file_hash(path: Path) -> str`; new module constant `_BINARY_SUFFIXES = frozenset({".pdf", ".xlsx"})`.

- [ ] **Step 1: Write the failing tests** — add to `TestComputeFileHash` in `carta/tests/test_lifecycle.py`:

```python
    def test_xlsx_raw_bytes_not_normalized(self):
        """.xlsx is a binary zip — two payloads differing only in CRLF vs LF bytes
        must hash differently (no normalization)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            a = Path(tmpdir) / "a.xlsx"
            b = Path(tmpdir) / "b.xlsx"
            a.write_bytes(b"PK\x03\x04payload\r\nmore")
            b.write_bytes(b"PK\x03\x04payload\nmore")
            assert compute_file_hash(a) != compute_file_hash(b)

    def test_xlsx_uppercase_suffix_also_raw(self):
        """Suffix dispatch is case-insensitive: .XLSX hashes raw too."""
        with tempfile.TemporaryDirectory() as tmpdir:
            a = Path(tmpdir) / "a.XLSX"
            b = Path(tmpdir) / "b.XLSX"
            a.write_bytes(b"PK\x03\x04x\r\n")
            b.write_bytes(b"PK\x03\x04x\n")
            assert compute_file_hash(a) != compute_file_hash(b)

    def test_csv_lf_normalized_like_text(self):
        """.csv is text — CRLF and LF variants of the same content hash identically
        (Windows-checkout line-ending churn must not trigger re-embeds)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            crlf = Path(tmpdir) / "crlf.csv"
            lf = Path(tmpdir) / "lf.csv"
            crlf.write_bytes(b"h1,h2\r\n1,2\r\n")
            lf.write_bytes(b"h1,h2\n1,2\n")
            assert compute_file_hash(crlf) == compute_file_hash(lf)

    def test_md_digest_pinned_across_refactor(self):
        """The binary-suffix refactor must not change any existing markdown digest
        (sidecar hash compatibility — zero-regression constraint)."""
        import hashlib
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "doc.md"
            f.write_bytes(b"# T\r\nbody")
            expected = hashlib.sha256(b"# T\nbody").hexdigest()
            assert compute_file_hash(f) == expected
```

- [ ] **Step 2: Run the tests to verify the xlsx ones fail**

Run: `python3 -m pytest carta/tests/test_lifecycle.py::TestComputeFileHash -v`
Expected: `test_xlsx_raw_bytes_not_normalized` and `test_xlsx_uppercase_suffix_also_raw` FAIL (hashes equal — CRLF got normalized); the csv and md tests already PASS.

- [ ] **Step 3: Implement** — in `carta/embed/lifecycle.py`, replace the body of `compute_file_hash` (keep the docstring, updating its Behavior block):

```python
# Binary formats hashed as raw bytes. CRLF→LF normalization on a binary container
# (pdf, xlsx-zip) is semantically wrong: two distinct payloads differing only in
# \r\n vs \n byte sequences would collide. Text formats keep LF-normalization so
# Windows-checkout line-ending churn does not trigger re-embeds.
_BINARY_SUFFIXES = frozenset({".pdf", ".xlsx"})


def compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of a file, with LF-normalization for text formats.

    Args:
        path: Path to the file to hash.

    Returns:
        Lowercase hexadecimal SHA256 hash string (64 characters).

    Behavior:
        - Binary formats (.pdf, .xlsx): read_bytes() raw, hash without normalization
        - Markdown, CSV, and other text files: read_bytes(), normalize CRLF → LF, hash
    """
    raw_bytes = path.read_bytes()

    if path.suffix.lower() in _BINARY_SUFFIXES:
        hash_obj = hashlib.sha256(raw_bytes)
    else:
        normalized = raw_bytes.replace(b"\r\n", b"\n")
        hash_obj = hashlib.sha256(normalized)

    return hash_obj.hexdigest()
```

- [ ] **Step 4: Run the full lifecycle tests**

Run: `python3 -m pytest carta/tests/test_lifecycle.py -v`
Expected: all PASS (including pre-existing `test_markdown_crlf_lf_same_hash`, `test_pdf_raw_bytes_not_normalized`).

- [ ] **Step 5: Full suite, then commit**

```bash
python3 -m pytest -q
git add carta/embed/lifecycle.py carta/tests/test_lifecycle.py
git commit -m "fix(lifecycle): hash binary suffixes (.pdf/.xlsx) raw, keep LF-normalization for text"
```

---

### Task 2: Extension-preserving sidecar naming + `spreadsheet` file_type

`sidecar_path()` (`carta/embed/induct.py:25`) strips the source extension, so `docs/data.csv` and `docs/data.md` would share one sidecar and auto-induction would silently skip whichever is seen second. New types get extension-preserving names (`data.csv.embed-meta.yaml`); `.md`/`.pdf` mappings are untouched. The two consumers that recompute the mapping — `iter_canonical_sidecars` (`induct.py:167`) and `_heal_sidecar_current_paths` (`pipeline.py:1180`) — must stay symmetric. `generate_sidecar_stub` (`induct.py:86`) also gains `file_type: "spreadsheet"` here so Task 6's discovery flip can't write wrong stubs.

**Files:**
- Modify: `carta/embed/induct.py:25-28` (sidecar_path), `:86` (file_type), `:167` (expected_rel)
- Modify: `carta/embed/pipeline.py:1178-1190` (`_heal_sidecar_current_paths`)
- Test: `carta/tests/test_induct.py`, `carta/tests/test_pipeline.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `SPREADSHEET_SUFFIXES: frozenset[str]` in `carta/embed/induct.py` (the single source of truth for `.csv`/`.xlsx`; Tasks 3 and 6 import it); `sidecar_path(file_path: Path, repo_root: Path) -> Path` with new-type behavior; stub `file_type` value `"spreadsheet"`.

- [ ] **Step 1: Write the failing tests** — add to `carta/tests/test_induct.py`:

```python
from carta.embed.induct import sidecar_path, write_sidecar


class TestSidecarNaming:
    """Extension-preserving sidecar names for spreadsheet types (spec: sidecar collision)."""

    def test_md_mapping_unchanged(self, tmp_path):
        f = tmp_path / "docs" / "data.md"
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "data.embed-meta.yaml")

    def test_pdf_mapping_unchanged(self, tmp_path):
        f = tmp_path / "docs" / "data.pdf"
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "data.embed-meta.yaml")

    def test_csv_preserves_extension(self, tmp_path):
        f = tmp_path / "docs" / "data.csv"
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "data.csv.embed-meta.yaml")

    def test_xlsx_preserves_extension_case_kept(self, tmp_path):
        f = tmp_path / "docs" / "Data.XLSX"
        # suffix *check* is case-insensitive; the filename itself is preserved as-is
        assert sidecar_path(f, tmp_path) == (
            tmp_path / ".carta" / "sidecars" / "docs" / "Data.XLSX.embed-meta.yaml")

    def test_same_stem_different_type_no_collision(self, tmp_path):
        md = sidecar_path(tmp_path / "docs" / "data.md", tmp_path)
        cs = sidecar_path(tmp_path / "docs" / "data.csv", tmp_path)
        assert md != cs

    def test_iter_canonical_accepts_extension_preserving_sidecar(self, tmp_path):
        src = tmp_path / "docs" / "data.csv"
        src.parent.mkdir(parents=True)
        src.write_text("a,b\n1,2\n")
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        stub = generate_sidecar_stub(src, tmp_path, cfg)
        write_sidecar(src, stub, tmp_path)
        found = [data["current_path"] for _, data in iter_canonical_sidecars(tmp_path)]
        assert "docs/data.csv" in found


class TestSpreadsheetFileType:
    def _stub(self, tmp_path, name):
        f = tmp_path / "docs" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333"}
        return generate_sidecar_stub(f, tmp_path, cfg)

    def test_csv_and_xlsx_are_spreadsheet(self, tmp_path):
        assert self._stub(tmp_path, "a.csv")["file_type"] == "spreadsheet"
        assert self._stub(tmp_path, "b.xlsx")["file_type"] == "spreadsheet"
        assert self._stub(tmp_path, "c.XLSX")["file_type"] == "spreadsheet"

    def test_md_uppercase_is_markdown(self, tmp_path):
        # latent case bug fixed while touching this line
        assert self._stub(tmp_path, "d.MD")["file_type"] == "markdown"
        assert self._stub(tmp_path, "e.md")["file_type"] == "markdown"

    def test_pdf_unchanged(self, tmp_path):
        assert self._stub(tmp_path, "f.pdf")["file_type"] == "pdf"
```

And add to `carta/tests/test_pipeline.py`:

```python
class TestHealExtensionPreservingSidecars:
    def test_heal_resolves_csv_sidecar_missing_current_path(self, tmp_path):
        from carta.embed.pipeline import _heal_sidecar_current_paths
        src = tmp_path / "docs" / "data.csv"
        src.parent.mkdir(parents=True)
        src.write_text("a,b\n1,2\n")
        sc = tmp_path / ".carta" / "sidecars" / "docs" / "data.csv.embed-meta.yaml"
        sc.parent.mkdir(parents=True)
        sc.write_text("slug: data\nstatus: pending\n")  # no current_path
        healed = _heal_sidecar_current_paths(tmp_path)
        assert healed == 1
        import yaml
        assert yaml.safe_load(sc.read_text())["current_path"] == "docs/data.csv"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest carta/tests/test_induct.py carta/tests/test_pipeline.py::TestHealExtensionPreservingSidecars -v`
Expected: `test_csv_preserves_extension`, `test_xlsx_preserves_extension_case_kept`, `test_same_stem_different_type_no_collision`, `test_iter_canonical_accepts_extension_preserving_sidecar`, spreadsheet file_type tests, uppercase-MD test, and the heal test FAIL. The `.md`/`.pdf` mapping tests PASS (guard rails).

- [ ] **Step 3: Implement in `carta/embed/induct.py`** — replace `sidecar_path` and add the constant + helper directly above it:

```python
# Spreadsheet source types. Their sidecar filename PRESERVES the source extension
# (data.csv -> data.csv.embed-meta.yaml): the legacy with_suffix() mapping strips
# it, so same-stem files (data.csv + data.md) would silently share one sidecar and
# auto-induction would skip the second. Legacy types (.md/.pdf) keep the historical
# mapping so existing sidecar state never migrates. Eventual unification: issue #89.
SPREADSHEET_SUFFIXES = frozenset({".csv", ".xlsx"})


def _sidecar_rel(rel: Path) -> Path:
    """Repo-relative source path -> repo-relative sidecar path (under sidecars root)."""
    if rel.suffix.lower() in SPREADSHEET_SUFFIXES:
        return rel.parent / (rel.name + ".embed-meta.yaml")
    return rel.with_suffix(".embed-meta.yaml")


def sidecar_path(file_path: Path, repo_root: Path) -> Path:
    """Return the canonical .carta/sidecars/ path for a source file's sidecar."""
    rel = file_path.relative_to(repo_root)
    return repo_root / ".carta" / "sidecars" / _sidecar_rel(rel)
```

In `generate_sidecar_stub`, replace line 86 (`file_type = "markdown" if file_path.suffix == ".md" else "pdf"`):

```python
    suffix = file_path.suffix.lower()
    if suffix == ".md":
        file_type = "markdown"
    elif suffix in SPREADSHEET_SUFFIXES:
        file_type = "spreadsheet"
    else:
        file_type = "pdf"
```

In `iter_canonical_sidecars`, replace line 167 (`expected_rel = Path(current_path).with_suffix(".embed-meta.yaml")`):

```python
        expected_rel = _sidecar_rel(Path(current_path))
```

- [ ] **Step 4: Implement in `carta/embed/pipeline.py`** — in `_heal_sidecar_current_paths`, after `parent_dirs = rel_from_sidecars.parent` (line ~1182) and before the `for ext in _SUPPORTED_EXTENSIONS:` loop, insert:

```python
        # Extension-preserving sidecars (data.csv.embed-meta.yaml) already carry
        # the full source filename in the stem — resolve directly.
        if Path(stem).suffix.lower() in SPREADSHEET_SUFFIXES:
            candidate = repo_root / parent_dirs / stem
            if candidate.exists():
                data["current_path"] = str(parent_dirs / stem)
                _update_sidecar(sc_path, data)
                healed += 1
            continue
```

Add `SPREADSHEET_SUFFIXES` to the existing `from carta.embed.induct import ...` line in pipeline.py.

- [ ] **Step 5: Run tests to verify pass**

Run: `python3 -m pytest carta/tests/test_induct.py carta/tests/test_pipeline.py -v`
Expected: all PASS.

- [ ] **Step 6: Full suite, then commit**

```bash
python3 -m pytest -q
git add carta/embed/induct.py carta/embed/pipeline.py carta/tests/test_induct.py carta/tests/test_pipeline.py
git commit -m "feat(induct): extension-preserving sidecar names + spreadsheet file_type for .csv/.xlsx"
```

---

### Task 3: Tabular extractor — CSV core (`carta/embed/tabular.py`)

The core new logic: deterministic per-sheet column classification (numeric / text / notes), markdown rendering of only the text-bearing content, and the companion-note helpers. This task covers `.csv`; `.xlsx` is Task 4.

**Files:**
- Create: `carta/embed/tabular.py`
- Create: `carta/embed/tests/test_tabular.py`

**Interfaces:**
- Consumes: `SPREADSHEET_SUFFIXES` is NOT needed here (dispatch keys on literal suffixes).
- Produces (Task 6 consumes all of these):
  - `extract_spreadsheet_text(path: Path) -> tuple[list[dict], dict]` — pages are `{"page": int, "text": str, "headings": [sheet_name]}` (text `""` for non-text-bearing sheets); meta is `{"companion_markdown": str, "sheet_names": list[str]}`.
  - `companion_rel_path(rel_path: Path) -> Path` — `docs/b.xlsx` → `.carta/companions/docs/b.xlsx.md` (extension appended, never replaced).
  - `write_companion(repo_root: Path, rel_path: Path, content: str) -> Optional[Path]` — fail-open, returns None on OSError.
  - `OpenpyxlMissing(RuntimeError)` — raised by Task 4's xlsx path.
  - Tuning constants `NUMERIC_THRESHOLD`, `FREE_TEXT_MIN_LEN`, `SAMPLE_ROWS`, `NOTES_HEADER_RE`.

- [ ] **Step 1: Write the failing tests** — create `carta/embed/tests/test_tabular.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify import failure**

Run: `python3 -m pytest carta/embed/tests/test_tabular.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'carta.embed.tabular'`.

- [ ] **Step 3: Implement** — create `carta/embed/tabular.py`:

```python
"""Deterministic text-bearing extraction from tabular sources (.csv, .xlsx).

Renders only the text-bearing content of a spreadsheet as markdown — text-column
values (lexical/BM25 targets, e.g. CAN frame names) and notes cells (dense
targets). Numeric columns contribute a header + range summary only, never cell
values. The full rendering (including numeric-only sheet stubs) doubles as the
companion-note transparency artifact under .carta/companions/.

Design: docs/superpowers/specs/2026-07-02-spreadsheet-companion-notes-design.md
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from typing import Iterator, Optional

# --- Tuning surface (pinned by tests) ---------------------------------------
NUMERIC_THRESHOLD = 0.8   # >= this fraction of non-empty cells numeric -> numeric column
FREE_TEXT_MIN_LEN = 60    # any cell this long promotes a text column to notes
SAMPLE_ROWS = 200         # classification samples at most this many data rows
NOTES_HEADER_RE = re.compile(r"note|description|comment|remark|desc", re.IGNORECASE)

_HEX_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")


class OpenpyxlMissing(RuntimeError):
    """openpyxl is not installed — .xlsx extraction unavailable."""


def _cell(value) -> str:
    return "" if value is None else str(value).strip()


def _is_numeric(value: str) -> bool:
    if _HEX_RE.match(value):
        return True
    try:
        float(value)
        return True
    except ValueError:
        return False


def _classify_columns(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Classify each column as 'numeric' | 'text' | 'notes'.

    Notes wins by header name; otherwise predominantly-numeric columns are
    numeric; a text column containing any long free-text cell is promoted to
    notes (full text preserved); everything else is text (distinct values kept).
    """
    kinds: list[str] = []
    sample = rows[:SAMPLE_ROWS]
    for i, header in enumerate(headers):
        if NOTES_HEADER_RE.search(header):
            kinds.append("notes")
            continue
        values = [r[i] for r in sample if i < len(r) and r[i]]
        if not values:
            kinds.append("numeric")  # nothing text-bearing in an empty column
            continue
        numeric = sum(1 for v in values if _is_numeric(v))
        if numeric / len(values) >= NUMERIC_THRESHOLD:
            kinds.append("numeric")
        elif any(len(v) >= FREE_TEXT_MIN_LEN for v in values):
            kinds.append("notes")
        else:
            kinds.append("text")
    return kinds


def _range_summary(values: list[str]) -> str:
    """Compact 'numeric lo–hi' summary; hex-formatted when every value is hex."""
    nums: list[float] = []
    all_hex = True
    for v in values:
        if _HEX_RE.match(v):
            nums.append(int(v, 16))
        elif _is_numeric(v):
            nums.append(float(v))
            all_hex = False
    if not nums:
        return "numeric"
    lo, hi = min(nums), max(nums)
    if all_hex:
        return f"numeric 0x{int(lo):X}–0x{int(hi):X}"

    def _fmt(n: float) -> str:
        return str(int(n)) if float(n).is_integer() else f"{n:g}"

    return f"numeric {_fmt(lo)}–{_fmt(hi)}"


def _render_sheet(source_name: str, sheet_name: str, raw_rows: list[list]) -> tuple[str, bool]:
    """Render one sheet. Returns (markdown_block, has_text_bearing_content).

    The block always includes the heading + column inventory (the companion
    stub); has_text_bearing_content is False for numeric-only/empty sheets,
    in which case the caller embeds nothing for this sheet.
    """
    rows = [[_cell(v) for v in row] for row in raw_rows]
    rows = [r for r in rows if any(r)]  # drop fully-empty rows
    lines = [f"# {source_name} — {sheet_name}", ""]
    if not rows:
        lines.append("(empty sheet)")
        return "\n".join(lines), False

    headers, data = rows[0], rows[1:]
    kinds = _classify_columns(headers, data)

    inventory = []
    for i, (header, kind) in enumerate(zip(headers, kinds)):
        label = header or f"col{i + 1}"
        if kind == "numeric":
            col_values = [r[i] for r in data if i < len(r) and r[i]]
            inventory.append(f"{label} ({_range_summary(col_values)})")
        else:
            inventory.append(f"{label} ({kind})")
    lines.append("Columns: " + ", ".join(inventory))

    # The first text column keys notes bullets (falls back to the row number).
    key_col = next((i for i, k in enumerate(kinds) if k == "text"), None)

    has_text = False
    for i, (header, kind) in enumerate(zip(headers, kinds)):
        label = header or f"col{i + 1}"
        if kind == "text":
            seen: dict[str, None] = {}  # insertion-ordered distinct values
            for r in data:
                if i < len(r) and r[i]:
                    seen.setdefault(r[i])
            if seen:
                has_text = True
                lines += ["", f"## {label}", ", ".join(seen)]
        elif kind == "notes":
            bullets = []
            for row_num, r in enumerate(data, start=2):  # +1 header, 1-based
                if i < len(r) and r[i]:
                    if key_col is not None and key_col < len(r) and r[key_col]:
                        key = r[key_col]
                    else:
                        key = f"row {row_num}"
                    bullets.append(f"- {key}: {r[i]}")
            if bullets:
                has_text = True
                lines += ["", f"## {label}", *bullets]
    return "\n".join(lines), has_text


def _csv_sheets(path: Path) -> Iterator[tuple[str, list[list]]]:
    # utf-8-sig strips a BOM; errors="replace" keeps stray bytes from dropping
    # the whole file (mirrors extract_markdown_text's read policy).
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        yield path.stem, list(csv.reader(f))


def _xlsx_sheets(path: Path) -> Iterator[tuple[str, list[list]]]:
    try:
        import openpyxl
    except ImportError as e:
        raise OpenpyxlMissing(
            f"cannot extract {path.name}: openpyxl is not installed — install it "
            f"with `pip install openpyxl` (pipx install: `pipx inject carta-cc openpyxl`)"
        ) from e
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            yield ws.title, [list(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def extract_spreadsheet_text(path: Path) -> tuple[list[dict], dict]:
    """Extract text-bearing pages from a .csv/.xlsx file.

    Returns (pages, meta):
        pages: one dict per sheet — {"page": i, "text": str, "headings": [sheet]}.
               text is "" for sheets with no text-bearing content; a workbook
               whose pages are ALL empty trips the pipeline's no_text_content path.
        meta:  {"companion_markdown": str, "sheet_names": list[str]} — the
               companion includes numeric-only sheet stubs (headers + ranges)
               for transparency even though those sheets embed nothing.

    Raises:
        OpenpyxlMissing: .xlsx requested but openpyxl is not installed.
    """
    sheets = _xlsx_sheets(path) if path.suffix.lower() == ".xlsx" else _csv_sheets(path)
    pages: list[dict] = []
    blocks: list[str] = []
    names: list[str] = []
    for page_num, (sheet_name, raw_rows) in enumerate(sheets, start=1):
        block, has_text = _render_sheet(path.name, sheet_name, raw_rows)
        blocks.append(block)
        names.append(sheet_name)
        pages.append({
            "page": page_num,
            "text": block if has_text else "",
            "headings": [sheet_name],
        })
    companion = (
        f"<!-- auto-generated by carta from {path.name}; do not edit — "
        f"regenerated on every re-embed -->\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )
    return pages, {"companion_markdown": companion, "sheet_names": names}


def companion_rel_path(rel_path: Path) -> Path:
    """Repo-relative companion path. Extension is APPENDED, never replaced —
    data.csv and data.xlsx in one directory must not collide."""
    return Path(".carta") / "companions" / rel_path.parent / (rel_path.name + ".md")


def write_companion(repo_root: Path, rel_path: Path, content: str) -> Optional[Path]:
    """Write the transparency artifact. Fail-open: it is not load-bearing."""
    target = repo_root / companion_rel_path(rel_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target
    except OSError as e:
        print(f"Warning: could not write companion note {target}: {e}",
              file=sys.stderr, flush=True)
        return None
```

Note on the CSV heading: for a `.csv`, the single "sheet name" is `path.stem` (`battery.csv` → heading `battery`), matching `test_csv_is_single_page_with_stem_heading`.

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest carta/embed/tests/test_tabular.py -v`
Expected: all PASS. If `test_mixed_sheet_keeps_text_drops_numeric_cells` fails on the StartBit assertion, check the en-dash: `_range_summary` uses `–` (U+2013); the test accepts both.

- [ ] **Step 5: Full suite, then commit**

```bash
python3 -m pytest -q
git add carta/embed/tabular.py carta/embed/tests/test_tabular.py
git commit -m "feat(tabular): deterministic text-bearing CSV extraction + companion helpers"
```

---

### Task 4: XLSX support via openpyxl (lazy) + packaging extra

`_xlsx_sheets` already exists from Task 3; this task proves it against real workbooks, pins the missing-dependency behavior, and adds the packaging extra.

**Files:**
- Modify: `pyproject.toml:18-22` (optional-dependencies)
- Test: `carta/embed/tests/test_tabular.py` (append)

**Interfaces:**
- Consumes: `extract_spreadsheet_text`, `OpenpyxlMissing` from Task 3.
- Produces: `pip install carta-cc[spreadsheet]` extra; verified multi-sheet page contract.

- [ ] **Step 1: Write the failing tests** — append to `carta/embed/tests/test_tabular.py`:

```python
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
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest carta/embed/tests/test_tabular.py::TestXlsxExtraction -v`
Expected: `test_missing_openpyxl_raises_actionable_error` PASSES immediately (Task 3 implemented the raise); the two workbook tests PASS if openpyxl is installed locally, otherwise SKIP. If they skip, install it into the test environment first: `python3 -m pip install "openpyxl>=3.1"` and re-run — they must PASS before commit.

- [ ] **Step 3: Add the packaging extra** — in `pyproject.toml`, replace the `[project.optional-dependencies]` block's `dev` line and add `spreadsheet`:

```toml
[project.optional-dependencies]
dev = ["pytest>=7.0", "openpyxl>=3.1"]
spreadsheet = ["openpyxl>=3.1"]
```

(`hybrid` and `visual` entries stay untouched.)

- [ ] **Step 4: Full suite, then commit**

```bash
python3 -m pytest -q
git add pyproject.toml carta/embed/tests/test_tabular.py
git commit -m "feat(tabular): xlsx extraction via lazy openpyxl + [spreadsheet] extra"
```

---

### Task 5: Register `no_text_content` in the status vocabulary

Sidecar `status` is a closed vocabulary; unregistered values fall into "other" buckets (the issue-#88 bug class). Register the new terminal status everywhere BEFORE anything can write it (Task 6 writes it).

**Files:**
- Modify: `carta/status.py` (`_CORPUS_STATUSES` line 23, `_gather_corpus` counts dict, `_corpus_line`)
- Modify: `carta/embed/pipeline.py` (`run_embed` summary dict + counting branch + docstring)
- Modify: `carta/cli.py` (`cmd_embed` post-run messages, after the `extraction_failed` warning)
- Modify: `carta/embed/repair.py:73` (bucket `no_text_content` re-embeds as `flagged`, not `queued_visual`)
- Test: `carta/tests/test_status_command.py`, `carta/tests/test_pipeline.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: status string literal `"no_text_content"`; `run_embed` summary key `"no_text_content": int`. Task 6's zero-text path writes this exact string.

- [ ] **Step 1: Write the failing tests** — add to `carta/tests/test_status_command.py`:

```python
class TestNoTextContentBucket:
    def test_no_text_content_counted_by_name_not_other(self, tmp_path):
        from carta.status import _gather_corpus
        sc_dir = tmp_path / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        (sc_dir / "ids.csv.embed-meta.yaml").write_text(
            "current_path: docs/ids.csv\nstatus: no_text_content\n")
        counts = _gather_corpus(tmp_path)
        assert counts["no_text_content"] == 1
        assert counts["other"] == 0

    def test_corpus_line_renders_no_text_bucket(self):
        from carta.status import _corpus_line
        co = {"total": 3, "done": 2, "pending": 0, "stale": 0,
              "extraction_failed": 0, "no_text_content": 1, "other": 0}
        line = _corpus_line(co, color=False)
        assert "1 no-text" in line
```

Add to `carta/tests/test_pipeline.py`:

```python
class TestRunEmbedNoTextContentCounting:
    def test_no_text_content_counted_in_summary(self, tmp_path):
        from unittest.mock import patch
        from carta.embed.pipeline import run_embed
        src = tmp_path / "docs" / "ids.csv"
        src.parent.mkdir(parents=True)
        src.write_text("MsgID\n0x100\n")
        sc_dir = tmp_path / ".carta" / "sidecars" / "docs"
        sc_dir.mkdir(parents=True)
        (sc_dir / "ids.csv.embed-meta.yaml").write_text(
            "current_path: docs/ids.csv\nstatus: pending\nslug: ids\ndoc_type: unknown\n")
        cfg = {"project_name": "p", "qdrant_url": "http://localhost:6333", "embed": {}}
        with patch("carta.embed.pipeline.QdrantClient"), \
             patch("carta.embed.pipeline.ensure_collection"), \
             patch("carta.embed.pipeline._embed_one_file",
                   return_value=(0, {"status": "no_text_content"})):
            summary = run_embed(tmp_path, cfg)
        assert summary["no_text_content"] == 1
        assert summary["embedded"] == 0
        assert summary["extraction_failed"] == 0
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest carta/tests/test_status_command.py::TestNoTextContentBucket carta/tests/test_pipeline.py::TestRunEmbedNoTextContentCounting -v`
Expected: all three FAIL (`KeyError: 'no_text_content'` / counted as "other" / counted as embedded).

- [ ] **Step 3: Implement in `carta/status.py`**

```python
_CORPUS_STATUSES = ("pending", "stale", "extraction_failed", "no_text_content")
```

In `_gather_corpus`, extend the counts dict:

```python
    counts = {"total": 0, "done": 0, "pending": 0, "stale": 0,
              "extraction_failed": 0, "no_text_content": 0, "other": 0}
```

In `_corpus_line`, after the `extraction_failed` part:

```python
    if co.get("no_text_content"):
        parts.append(f"{co['no_text_content']} no-text")
```

(`.get` keeps older callers passing the pre-change dict shape working.)

- [ ] **Step 4: Implement in `carta/embed/pipeline.py`** — in `run_embed`, extend the summary dict:

```python
    summary: dict = {"embedded": 0, "skipped": 0, "extraction_failed": 0,
                     "no_text_content": 0,
                     "failed": [], "partial": [], "errors": [], "timed_out": []}
```

Replace the counting branch `elif st == "extraction_failed":` with:

```python
                elif st in ("extraction_failed", "no_text_content"):
                    summary[st] += 1
                    perf_status = "ok"
                    status.file_done(embedded=1, chunks=count)
```

Update the `run_embed` docstring's Returns block to include `"no_text_content": int`.

- [ ] **Step 5: Implement in `carta/cli.py`** — in `cmd_embed`, directly after the `failed_extractions` warning block (anchor: the print containing `"scanned PDFs? OCR may be required"`), add:

```python
    no_text = summary.get("no_text_content", 0)
    if no_text:
        print(
            f"\nNote: {no_text} spreadsheet file(s) contained no text-bearing cells "
            f"(numeric-only data is deliberately not indexed) — flagged no_text_content.",
            file=sys.stderr,
        )
```

- [ ] **Step 6: Implement in `carta/embed/repair.py`** — replace line 73 (`if sc.get("status") == "extraction_failed":`):

```python
                # Both are terminal zero-chunk verdicts, not queued-visual PDFs.
                if sc.get("status") in ("extraction_failed", "no_text_content"):
```

(No test — `no_text_content` sidecars never enter `report["affected_files"]` because the integrity count-mismatch check fires only on `status == "embedded"`; this line only classifies a repair re-embed's outcome correctly.)

- [ ] **Step 7: Run tests to verify pass, full suite, commit**

```bash
python3 -m pytest carta/tests/test_status_command.py carta/tests/test_pipeline.py -v
python3 -m pytest -q
git add carta/status.py carta/embed/pipeline.py carta/cli.py carta/embed/repair.py \
        carta/tests/test_status_command.py carta/tests/test_pipeline.py
git commit -m "feat(status): register no_text_content across status vocabulary consumers"
```

---

### Task 6: Pipeline activation — discovery, dispatch, companion, zero-text, scanner

The switch-on task: `.csv`/`.xlsx` enter `_SUPPORTED_EXTENSIONS`, the extraction dispatch grows a spreadsheet branch (all comparisons lowercased), companions get written fail-open, chunks carry `derived`/`companion_path`, zero-text spreadsheets get `no_text_content`, and the scanner's independent extension set learns the new types.

**Files:**
- Modify: `carta/embed/pipeline.py:74` (`_SUPPORTED_EXTENSIONS`), `:86-88` (`_iter_inductable_files` — `.carta` guard), `:348-368` (dispatch), `:386-395` (metadata), `:406-421` (zero-text)
- Modify: `carta/scanner/scanner.py:641` (`_EMBED_EXTENSIONS`)
- Create: `carta/tests/test_tabular_pipeline.py`
- Test: `carta/scanner/tests/test_scanner.py` (append)

**Interfaces:**
- Consumes: `SPREADSHEET_SUFFIXES` (Task 2, from `carta.embed.induct`); `extract_spreadsheet_text`, `write_companion`, `companion_rel_path`, `OpenpyxlMissing` (Task 3, from `carta.embed.tabular`); status `"no_text_content"` + summary key (Task 5).
- Produces: end-to-end spreadsheet embedding.

- [ ] **Step 1: Write the failing tests** — create `carta/tests/test_tabular_pipeline.py`:

```python
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
```

Append to `carta/scanner/tests/test_scanner.py`:

```python
class TestSpreadsheetInduction:
    def test_spreadsheets_flagged_for_induction(self, tmp_path):
        from carta.scanner.scanner import check_embed_induction_needed
        ref = tmp_path / "docs" / "reference"
        ref.mkdir(parents=True)
        (ref / "data.xlsx").write_bytes(b"PK\x03\x04")
        (ref / "data.csv").write_text("a,b\n1,2\n")
        issues = check_embed_induction_needed(tmp_path, {})
        docs = {i["doc"] for i in issues}
        assert "docs/reference/data.xlsx" in docs
        assert "docs/reference/data.csv" in docs
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest carta/tests/test_tabular_pipeline.py carta/scanner/tests/test_scanner.py -v`
Expected: every new test FAILS (csv routed to `extract_pdf_text` and errors or yields nothing; discovery misses `.csv`; scanner ignores spreadsheets). Pre-existing scanner tests PASS.

- [ ] **Step 3: Implement discovery in `carta/embed/pipeline.py`**

```python
_SUPPORTED_EXTENSIONS = [".pdf", ".md", ".csv", ".xlsx"]
```

In `_iter_inductable_files`, add the `.carta` guard inside the loop:

```python
    for p in docs_root.rglob("*"):
        if ".carta" in p.parts:
            continue  # derived artifacts (companions, sidecars) are never sources
        if p.is_file() and p.suffix.lower() in _SUPPORTED_EXTENSIONS_SET:
            yield p
```

Add imports near the other `carta.embed` imports:

```python
from carta.embed.tabular import (
    extract_spreadsheet_text, write_companion, companion_rel_path, OpenpyxlMissing,
)
```

- [ ] **Step 4: Implement the dispatch in `_embed_one_file`** — replace the block at lines 347-368 (`if file_path.suffix == ".md": ... else: pages = extract_pdf_text(file_path); frontmatter_meta = {}`) with:

```python
    suffix = file_path.suffix.lower()
    tabular_meta: dict = {}
    if suffix == ".md":
        pages, frontmatter_meta = extract_markdown_text(file_path)
    elif suffix in SPREADSHEET_SUFFIXES:
        try:
            pages, tabular_meta = extract_spreadsheet_text(file_path)
        except OpenpyxlMissing as e:
            print(f"Warning: {e}", file=sys.stderr, flush=True)
            # Leave the sidecar re-pickable: the file is retried once openpyxl
            # is installed. No mtime/hash fields are stamped.
            return 0, {"status": "pending"}
        frontmatter_meta = {}
        # Companion note: transparency artifact, fail-open (not load-bearing).
        write_companion(
            repo_root, file_path.relative_to(repo_root),
            tabular_meta.get("companion_markdown", ""),
        )
    elif suffix == ".pdf" and cfg.get("embed", {}).get("two_pass_visual", True):
        try:
            analyzer = PageAnalyzer(cfg)
            pages, _page_classes_from_extraction = extract_pdf_text_and_classify(file_path, analyzer)
        except Exception as _cls_exc:
            # Classification failed: fall back to text-only extraction and skip inline vision
            # (fail closed — do not escalate to the heavy VLM path).
            print(
                f"Warning: two_pass_visual page classification failed for {file_path}: {_cls_exc}; "
                f"pages left unclassified — skipping inline vision for this file",
                file=sys.stderr,
                flush=True,
            )
            pages = extract_pdf_text(file_path)
            _page_classes_from_extraction = None  # signals: skip both inline vision AND two-pass queue
        frontmatter_meta = {}
    else:
        pages = extract_pdf_text(file_path)
        frontmatter_meta = {}
```

(The PDF branch body is unchanged; only its condition now uses the lowercased `suffix`, which also gives uppercase `.PDF` the two-pass path it was always meant to have.)

- [ ] **Step 5: Implement metadata + zero-text** — after the metadata dict (anchor: `"doc_generation": generation,` at line ~392), add:

```python
    if suffix in SPREADSHEET_SUFFIXES:
        metadata["derived"] = "spreadsheet"
        metadata["companion_path"] = str(
            companion_rel_path(file_path.relative_to(repo_root)))
```

Replace the zero-text early-return block (anchor: `if expected_text == 0 and file_path.suffix != ".pdf":` at line ~406) with:

```python
    if expected_text == 0 and suffix != ".pdf":
        if suffix in SPREADSHEET_SUFFIXES:
            zero_status = "no_text_content"
            print(
                f"Note: {file_path.name}: no text-bearing cells — nothing embedded "
                f"(numeric-only data is deliberately not indexed)",
                flush=True,
            )
        else:
            zero_status = "extraction_failed"
            print(
                f"Warning: {file_path.name}: 0 extractable characters — "
                f"skipped (empty or unreadable file)",
                flush=True,
            )
        return 0, {
            "status": zero_status,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
            "chunk_count": 0,
            "image_count": 0,
            "image_chunks": 0,
            "file_mtime": os.path.getmtime(str(file_path)),
            "visual_pages": 0,
            "_vision_events": [],
        }
```

- [ ] **Step 6: Handle the `"pending"` outcome in `run_embed`'s counting** — in the counting chain from Task 5, add before the final `else`:

```python
                elif st == "pending":
                    # spreadsheet skipped for a missing optional dependency —
                    # stays re-pickable, count as skipped
                    summary["skipped"] += 1
                    perf_status = "skip"
                    status.file_done(skipped=1)
```

- [ ] **Step 7: Implement in `carta/scanner/scanner.py:641`**

```python
_EMBED_EXTENSIONS = frozenset(
    [".pdf", ".m4a", ".mp3", ".wav", ".aac", ".csv", ".xlsx"])
```

- [ ] **Step 8: Run tests to verify pass**

Run: `python3 -m pytest carta/tests/test_tabular_pipeline.py carta/scanner/tests/test_scanner.py carta/tests/test_pipeline.py -v`
Expected: all PASS.

- [ ] **Step 9: Full suite, then commit**

```bash
python3 -m pytest -q
git add carta/embed/pipeline.py carta/scanner/scanner.py \
        carta/tests/test_tabular_pipeline.py carta/scanner/tests/test_scanner.py
git commit -m "feat(embed): activate .csv/.xlsx sources — discovery, dispatch, companion notes, no_text_content"
```

---

### Task 7: `remember --about` file association

Attach a human gotcha to a specific file: `about` rides the note's frontmatter, plumbed through CLI and MCP. Warn-don't-fail on a missing target (the association is metadata, not a foreign key).

**Files:**
- Modify: `carta/memory/capture.py:32-84` (`capture_note`)
- Modify: `carta/cli.py:714-729` (`cmd_remember`), `:1084-1094` (parser)
- Modify: `carta/mcp/server.py:501-539` (`_remember`, `carta_remember`)
- Test: `carta/tests/test_capture.py`, `carta/tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (independent unit).
- Produces: `capture_note(cfg, repo_root, text, *, note_type, title="", tags=None, about=None)`; CLI flag `--about <path>`; MCP param `about: str | None`.

- [ ] **Step 1: Write the failing tests** — add to `carta/tests/test_capture.py`:

```python
class TestAboutAssociation:
    def test_about_recorded_in_frontmatter(self, tmp_path):
        target = tmp_path / "docs" / "battery.xlsx"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"PK")
        out, _ = _capture(tmp_path, about="docs/battery.xlsx")
        fm = yaml.safe_load((tmp_path / out["path"]).read_text().split("---")[1])
        assert fm["about"] == "docs/battery.xlsx"

    def test_about_absolute_path_normalized_to_repo_relative(self, tmp_path):
        target = tmp_path / "docs" / "battery.xlsx"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"PK")
        out, _ = _capture(tmp_path, about=str(target))
        fm = yaml.safe_load((tmp_path / out["path"]).read_text().split("---")[1])
        assert fm["about"] == "docs/battery.xlsx"

    def test_about_missing_target_warns_but_captures(self, tmp_path, capsys):
        out, _ = _capture(tmp_path, about="docs/nonexistent.xlsx")
        assert "does not exist" in capsys.readouterr().err
        fm = yaml.safe_load((tmp_path / out["path"]).read_text().split("---")[1])
        assert fm["about"] == "docs/nonexistent.xlsx"

    def test_no_about_no_frontmatter_key(self, tmp_path):
        out, _ = _capture(tmp_path)
        fm = yaml.safe_load((tmp_path / out["path"]).read_text().split("---")[1])
        assert "about" not in fm
```

(`_capture` is the existing helper in this file — pass `about` through its `**kw`.)

Add to `carta/tests/test_mcp_server.py`:

```python
class TestRememberAbout:
    def test_about_param_plumbs_to_capture_note(self, tmp_path):
        from unittest.mock import patch
        from carta.mcp import server as mcp_server_mod
        with patch.object(mcp_server_mod, "_load_cfg",
                          return_value={"project_name": "p", "qdrant_url": "u"}), \
             patch.object(mcp_server_mod, "_repo_root_from_cfg", return_value=tmp_path), \
             patch("carta.memory.capture.capture_note",
                   return_value={"path": "docs/notes/x.md", "collection": "p_notes",
                                 "chunks": 1}) as cap:
            out = mcp_server_mod._remember(
                "gotcha", note_type="quirk", about="docs/battery.xlsx")
        assert out["status"] == "ok"
        assert cap.call_args.kwargs["about"] == "docs/battery.xlsx"
```

(Match the patching style already used in `carta/tests/test_mcp_server.py` for `_load_cfg`/`_repo_root_from_cfg`; if existing tests patch differently, mirror them.)

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest carta/tests/test_capture.py::TestAboutAssociation carta/tests/test_mcp_server.py::TestRememberAbout -v`
Expected: FAIL with `TypeError: capture_note() got an unexpected keyword argument 'about'` (and the MCP test similarly).

- [ ] **Step 3: Implement in `carta/memory/capture.py`** — extend the signature:

```python
def capture_note(cfg: dict, repo_root: Path, text: str, *,
                 note_type: str, title: str = "",
                 tags: list[str] | None = None,
                 about: str | None = None) -> dict:
```

Document the new arg in the docstring Args block:

```python
        about: optional path of the file this note is about; recorded in the
            frontmatter as a repo-relative path. Warns (does not fail) if the
            target does not exist.
```

After the `if tags:` block that builds `frontmatter`, add:

```python
    if about:
        import sys
        about_p = Path(about)
        if about_p.is_absolute():
            try:
                about_p = about_p.relative_to(repo_root)
            except ValueError:
                pass  # outside the repo — record as given
        about_rel = about_p.as_posix()
        if not (Path(repo_root) / about_rel).exists():
            print(f"Warning: --about target does not exist: {about_rel} "
                  f"(note captured anyway)", file=sys.stderr)
        frontmatter["about"] = about_rel
```

(Move `import sys` to the module's import block instead of inline — shown inline here only for placement clarity.)

- [ ] **Step 4: Implement in `carta/cli.py`** — in the parser section (after the `--tags` argument at line ~1094):

```python
    remember_p.add_argument(
        "--about", default="",
        help="Path of the file this note is about (recorded in frontmatter)")
```

In `cmd_remember`, pass it through:

```python
        result = capture_note(cfg, repo_root, args.text, note_type=args.type,
                              title=args.title, tags=tags,
                              about=(args.about or None))
```

- [ ] **Step 5: Implement in `carta/mcp/server.py`** — extend `_remember`:

```python
def _remember(text: str, *, note_type: str = "helpful-note", title: str = "",
              tags: list[str] | None = None, about: str | None = None) -> dict:
```

and its `capture_note` call:

```python
        result = capture_note(cfg, repo_root, text, note_type=note_type,
                              title=title, tags=tags, about=about)
```

Extend the tool:

```python
@mcp_server.tool()
def carta_remember(
    text: str,
    note_type: str = "helpful-note",
    title: str = "",
    tags: list[str] | None = None,
    about: str | None = None,
) -> dict:
```

and add one line to its docstring:

```
    Pass about=<repo-relative path> to associate the note with a specific file
    (e.g. the spreadsheet a gotcha applies to); it is recorded in frontmatter.
```

and forward it:

```python
    return _remember(text, note_type=note_type, title=title, tags=tags, about=about)
```

- [ ] **Step 6: Run tests to verify pass, full suite, commit**

```bash
python3 -m pytest carta/tests/test_capture.py carta/tests/test_mcp_server.py -v
python3 -m pytest -q
git add carta/memory/capture.py carta/cli.py carta/mcp/server.py \
        carta/tests/test_capture.py carta/tests/test_mcp_server.py
git commit -m "feat(remember): --about file association in frontmatter (CLI + MCP)"
```

---

### Task 8: BM25 tokenization pin + docs

The design leans on BM25 matching underscore identifiers (`TMS_CoolantTemp`) both whole and as fragments (`TMS`). Pin the fastembed tokenizer behavior with a test instead of assuming it, and update the user-facing docs.

**Files:**
- Test: `carta/embed/tests/test_sparse.py` (append)
- Modify: `CLAUDE.md` (Carta surface table), `README.md`, `docs/field-notes.md`

**Interfaces:**
- Consumes: `embed_sparse_document`, `embed_sparse_query` (`carta/embed/sparse.py` — both return `SparseVec(indices, values)`).
- Produces: documentation only.

- [ ] **Step 1: Write the tokenization pin test** — append to `carta/embed/tests/test_sparse.py`:

```python
def test_underscore_identifier_queries_share_tokens_with_document():
    """Spreadsheet retrieval leans on BM25 matching identifiers like
    TMS_CoolantTemp both whole and as fragments (TMS). Pin that the fastembed
    Qdrant/bm25 tokenizer gives the query and document overlapping token indices
    for both query shapes."""
    pytest.importorskip("fastembed")
    from carta.embed.sparse import embed_sparse_document, embed_sparse_query
    doc = embed_sparse_document(
        "Signal: BMS_PackVoltage, BMS_PackCurrent, TMS_CoolantTemp, TMS_FlowRate")
    for query in ("TMS_CoolantTemp", "TMS"):
        qv = embed_sparse_query(query)
        overlap = set(qv.indices) & set(doc.indices)
        assert overlap, f"query {query!r} shares no BM25 tokens with the document"
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest carta/embed/tests/test_sparse.py -v`
Expected: PASS (or SKIP if fastembed missing — install with `python3 -m pip install "fastembed>=0.4"` and re-run; it must PASS before commit). **If the `"TMS"` case genuinely fails** after the model runs: that is a real finding — the tokenizer keeps identifiers whole. Do not force the test green; keep the passing whole-identifier case, drop the fragment case, and record the behavior in `docs/field-notes.md` ("fragment queries like `TMS` do not lexically match `TMS_CoolantTemp`; use the full identifier or rely on dense retrieval").

- [ ] **Step 3: Update `CLAUDE.md`** — in the "Carta surface" CLI table, replace the `embed` row's Purpose with:

```
Extract/chunk/embed pending docs → Qdrant. `--visual` drains image-heavy pages (two-pass); `--repair` re-embeds damaged points. `.xlsx`/`.csv` sources embed text-bearing cells only (frame names + notes; numerics stay out), mirrored to `.carta/companions/` |
```

and the `remember` row's Purpose with:

```
Capture a curated note (quirk / bug-note / helpful-note); `--about <file>` associates it with a source file |
```

In the "Sidecars" section, append:

```
Spreadsheet sources (`.csv`/`.xlsx`) use extension-preserving sidecar names
(`data.csv.embed-meta.yaml`) so same-stem files never collide; `.md`/`.pdf`
keep the legacy extension-stripped names.
```

- [ ] **Step 4: Update `README.md`** — find the `carta embed` entry (`grep -n "embed" README.md | head`) and append to its description: "Supports `.pdf`, `.md`, `.csv`, `.xlsx` (xlsx needs the `spreadsheet` extra: `pipx inject carta-cc openpyxl`)." Find the `carta remember` entry and append: "`--about <file>` records which file the note is about."

- [ ] **Step 5: Update `docs/field-notes.md`** — append under the operating-guide entries:

```markdown
## Spreadsheets (.csv/.xlsx)

- Carta embeds only the **text-bearing** content of spreadsheets: text-column
  values (e.g. CAN frame names) and notes cells. Numeric columns contribute a
  header + range summary only. Numeric-only workbooks get sidecar status
  `no_text_content` — that is healthy, not an error.
- Search hits cite the **workbook**; the rendering that was embedded is
  inspectable at `.carta/companions/<path>.md` (regenerated on re-embed — do
  not edit).
- `.xlsx` needs `openpyxl` (`pipx inject carta-cc openpyxl`); without it the
  file is skipped with a warning and retried once installed.
- To attach a durable human gotcha to a file:
  `carta remember "..." --type quirk --about docs/battery.xlsx`.
```

- [ ] **Step 6: Full suite, then commit**

```bash
python3 -m pytest -q
git add carta/embed/tests/test_sparse.py CLAUDE.md README.md docs/field-notes.md
git commit -m "test(sparse): pin BM25 identifier tokenization; docs for spreadsheet sources"
```

---

### Follow-ups (tracked, not part of this plan)

- [ ] Eval growth (#19): add ≥1 spreadsheet-sourced retrieval case to the ET-embed eval corpus (e.g. "what is the scaling on TMS_CoolantTemp?") — lives in the petsense project, run after this branch is installed there.
- [ ] Issue #89: unify `.md`/`.pdf` sidecar names onto the extension-preserving scheme (migration).
- [ ] LFS-pointer guard for `run_embed_file` (targeted embed path) — file an issue if spreadsheet corpora adopt LFS.
