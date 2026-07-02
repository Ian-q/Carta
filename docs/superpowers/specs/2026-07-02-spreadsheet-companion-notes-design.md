---
title: Spreadsheet companion notes — indexing text-bearing tabular data
date: 2026-07-02
status: draft
---

# Spreadsheet companion notes

## Problem

Carta ingests exactly two source types today — `.pdf` and `.md`
(`carta/embed/pipeline.py:73`, `_SUPPORTED_EXTENSIONS = [".pdf", ".md"]`).
Tabular sources (`.xlsx`, `.csv`) are invisible to the index.

Concrete failure (ET-embed, CAN dictionary): an agent working on battery/BMS CAN
signals hit a signal it didn't recognize, could not find it in CLAUDE.md or the
markdown docs, and could not `carta search` for it — because the authoritative
description lived in an Excel workbook that was never embedded. The human had to
open the workbook, `Ctrl+F` for `TMS` (thermal management system), scan the
matching cells, and hand the frame names and notes back to the agent.

Two of the three things the human retrieved were **text-bearing**: the CAN frame
names (lexical tokens like `TMS_CoolantTemp`) and the free-text notes cells. The
rest of the sheet — the numeric columns — was noise the human deliberately
skipped. That split is the whole design.

## Goals

- Make the **text-bearing** content of `.xlsx` / `.csv` files discoverable through
  the existing hybrid search: BM25 handles the lexical `Ctrl+F` case (frame names),
  dense handles the semantic case ("anything about thermal management?").
- Keep numeric/tabular noise **out** of the dense index.
- Let a human attach durable "x marks the spot" gotchas to a specific file, reusing
  the existing note-capture feature rather than inventing a parallel one.
- Zero regression to existing `.pdf` / `.md` retrieval and sidecar state
  (per CLAUDE.md compatibility constraint).

## Non-goals (YAGNI)

- **No first-class binary embedding.** We do not add `.xlsx`/`.csv` to a naive
  "chunk it like a document" path. Dense-embedding packed numeric rows is noise,
  and a binary workbook has no stable text to anchor citations or change-detection
  to. (This was "Approach C" in brainstorming — explicitly rejected.)
- **No LLM summarization** of tables. Extraction is deterministic, always fresh,
  cheap, and offline. (Aligns with the local-only constraint.)
- **No in-file human-notes region.** Companion notes are churnable and
  machine-owned; mixing a precious hand-edited region into a regenerated file
  under `.carta/` is fragile (clobber risk + fence-merge bugs). Human notes live
  separately via `remember` (see below).
- **No `carta focus` support for spreadsheets** in v1 (no page-image / outline for
  workbooks). Search hits that cite the parent workbook are sufficient for the
  reported need.
- **No other formats** (`.docx`, Google Sheets, `.ods`, `.tsv`) in v1.

## Approach

Two clean, independent units:

### Unit 1 — Generated companion note (machine-owned)

The spreadsheet becomes a **tracked source file**. During its extract step, a new
deterministic extractor parses the workbook, renders only the text-bearing content
as markdown, writes that rendering to a companion file under `.carta/`, and returns
the rendered text to the existing pipeline for chunking/embedding.

- The **spreadsheet** (not the companion `.md`) is the unit of discovery, hashing,
  and the `.embed-meta.yaml` sidecar. This means change-detection and regeneration
  come for free from the existing generation/lifecycle machinery: when the workbook
  changes, its hash changes, it re-inducts, re-extracts, and the companion note is
  regenerated — no new sync mechanism.
- Embedded chunks carry `file_path = <spreadsheet rel path>`, so **search hits cite
  the workbook**, not the derived file.
- The companion `.md` under `.carta/companions/<rel>.md` is a transparency /
  inspection artifact ("what did Carta actually index from my workbook?") whose
  content is exactly what was embedded. It is freely deletable and regenerated;
  it is **not** independently discovered or embedded.

### Unit 2 — Human gotchas via `remember --about`

Durable, hand-written notes already have a home: `carta remember`
(`carta/memory/capture.py`) writes a frontmatter markdown file into
`docs/quirks/` or `docs/notes/` and embeds it. We add one optional association:

- New optional `--about <path>` (CLI) / `about` param (MCP `carta_remember`) that
  records the target file in the note's frontmatter as `about: <repo-rel path>`.
- The note stays human-facing in `docs/`, durable, and already searchable.

**The two units are not physically merged.** Both carry the parent workbook's path
in their metadata, both are in the index, so a search for `TMS` surfaces whichever
is relevant — the extracted frame names *and* any "x marks the spot" quirk — and
both cite the workbook. The index does the merging; no file-region convention is
needed.

## Architecture & integration points

| Concern | Change | Location |
|---|---|---|
| Discovery | Add `.xlsx`, `.csv` to the supported set | `pipeline.py:73` `_SUPPORTED_EXTENSIONS` |
| Extraction dispatch | New branch: `.xlsx`/`.csv` → `extract_spreadsheet_text()` | `pipeline.py:347-367` |
| Extractor (new) | `extract_spreadsheet_text(path) -> (pages, meta)` | new module `carta/embed/tabular.py` |
| Companion render | Write `.carta/companions/<rel>.md`; return rendered text as `pages` | `carta/embed/tabular.py` |
| Citation metadata | Chunks already inherit `file_path` = source path → workbook. Add `derived: "spreadsheet"` + `companion_path` marker | `pipeline.py:387` metadata dict |
| Hashing | `.xlsx` → raw bytes (like PDF); `.csv` → LF-normalized (like text) | `carta/embed/lifecycle.py` (currently CRLF-normalizes all non-PDF — bug for binary xlsx) |
| Sidecar stub | `file_type` gains `"spreadsheet"` (or per-suffix) | `induct.py:86` |
| Discovery exclusion | Ensure `.carta/companions/*.md` is never picked up by the `.md` source sweep (it is derived, attributed to its parent) | `_iter_inductable_files` / docs_root exclusions |
| Zero-text status | A workbook with no text-bearing cells → a `no_text_content` status, not the harsher `extraction_failed` | `pipeline.py:405` |
| Human notes | `about` frontmatter field + `--about` flag / MCP param | `capture.py:32`, CLI, `mcp/server.py` |

The `pages` contract consumed by `chunk_text` is honored: **one logical page per
sheet** (a `.csv` is a single page). This gives per-sheet anchoring for free.

## Extraction heuristic (the core new logic)

Deterministic, per sheet:

1. **Header row**: first non-empty row is treated as headers.
2. **Column classification** by sampling values down each column:
   - Predominantly numeric → **numeric column**: emit only the header name and a
     compact range summary (e.g. `MsgID (numeric 0x100–0x7FF)`), never the cell
     values.
   - Predominantly string → **text column**: emit the distinct values (these are
     the frame names — the BM25 targets).
   - Column whose header matches `/note|description|comment|remark|desc/i`, **or**
     any cell that is a long free-text string → **notes**: emit the full text
     (these are the dense-search targets).
3. **Render** to markdown: sheet name as a heading, a one-line column inventory,
   then the text-column values and the notes. Numeric-only sheets render to a short
   stub (headers + ranges) and typically trip the `no_text_content` path.

Illustrative output:

```markdown
# battery.xlsx — CAN_Signals (auto-generated by carta; do not edit)

Columns: MsgID (numeric 0x100–0x7FF), Signal (text), StartBit (numeric 0–63), Notes (text)

## Signal
BMS_PackVoltage, BMS_PackCurrent, TMS_CoolantTemp, TMS_FlowRate, ...

## Notes
- TMS_CoolantTemp: only valid when pump enabled (see rows 40–60)
- ...
```

Thresholds (what counts as "predominantly", the free-text length cutoff, the notes
header pattern) are the tuning surface and will be pinned by tests.

## Data flow

```
carta embed
  └─ discover battery.xlsx  (new: matches _SUPPORTED_EXTENSIONS)
       └─ hash raw bytes → sidecar generation bump on change
       └─ extract_spreadsheet_text(battery.xlsx)
            ├─ parse workbook (openpyxl / stdlib csv)
            ├─ classify columns, render text-bearing markdown
            ├─ write .carta/companions/battery.xlsx.md   (transparency artifact)
            └─ return pages  (one per sheet)
       └─ chunk → embed → upsert   (metadata.file_path = "battery.xlsx")
```

Search / hook are unchanged — they retrieve the new chunks like any others, and
citations resolve to `battery.xlsx`.

## Dependencies

- `.csv` — stdlib `csv`, no new dependency.
- `.xlsx` — `openpyxl` (read-only mode), the minimal well-maintained reader.
  **Not** `pandas` (heavy). This is a library dependency, not new infra
  (consistent with the tech-stack constraint). Import lazily so a missing
  `openpyxl` degrades to a clear "install openpyxl to embed .xlsx" message rather
  than breaking the pipeline for `.pdf`/`.md` users.

## Error handling

- Corrupt/unreadable workbook → warn to stderr, mark the file's sidecar
  `extraction_failed`, continue the batch (fail-open, matches PDF behavior).
- Missing `openpyxl` → skip `.xlsx` files with an actionable message; `.csv` and
  existing types are unaffected.
- No text-bearing content → `no_text_content` status (distinct from
  `extraction_failed`); nothing embedded, no error.
- Companion-file write failure → warn but still embed the in-memory rendering
  (the artifact is transparency, not load-bearing).

## Testing (TDD)

Failing-test-first, per feature:

- Extractor unit tests (`carta/embed/tests/`): numeric-only sheet → stub +
  `no_text_content`; mixed sheet → frame names present, numeric cell values absent;
  notes column fully preserved; multi-sheet → one page per sheet; CSV → single page;
  hex-formatted numeric columns summarized as ranges.
- Pipeline integration: `.xlsx`/`.csv` discovered and embedded; chunk
  `metadata.file_path` equals the workbook path; companion `.md` written under
  `.carta/companions/` and **not** double-discovered by the `.md` sweep.
- Change detection: editing the workbook bumps the sidecar generation and
  regenerates the companion note; unchanged workbook is skipped.
- Hashing: `.xlsx` hashed as raw bytes; `.csv` LF-normalized.
- `remember --about`: frontmatter carries `about`; MCP param plumbs through.
- Regression: existing `.pdf`/`.md` extraction/sidecar tests stay green.

## Eval

Retrieval-quality changes are validated against the ET-embed eval corpus (per
CLAUDE.md). New workbook content only *adds* documents, but we still run the eval to
confirm no regression on the existing 62-question set, and — tying into the #19
eval-growth rescope — add at least one spreadsheet-sourced retrieval case
(e.g. "what is the scaling on `TMS_CoolantTemp`?") so this path is guarded going
forward.

## Rollout / scope

Single implementation plan. Order: (1) extractor module + tests, (2) pipeline
discovery/dispatch/hashing/companion-write wiring, (3) `remember --about`, (4) eval
case + docs (CLAUDE.md Carta-surface table, README command notes). No migration —
existing collections and sidecars are untouched; the feature only adds new source
files.

## Future (out of scope, noted)

- `carta focus` for workbooks (per-sheet outline; table cells as images).
- LLM-assisted column semantics for messy sheets where header heuristics fail.
- Additional formats (`.tsv`, `.ods`, `.docx`).
- Surfacing "human notes about this file" inline in the companion rendering (a
  read-time join on the `about` metadata, not a physical merge).
