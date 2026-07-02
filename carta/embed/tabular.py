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
