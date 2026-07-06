---
title: Spreadsheet companion notes — indexing text-bearing tabular data
date: 2026-07-02
status: draft
audited: 2026-07-02 (claims verified against code; see Amendments section)
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
  come for free from the existing generation/lifecycle machinery
  (`run_embed_file`'s hash → generation-bump path): when the workbook changes, its
  hash changes, re-embedding re-extracts it and the companion note is regenerated —
  no new sync mechanism. Precision on the *trigger*: the batch `carta embed` run
  only re-picks `pending`/`embed_failed`/`partial` sidecars
  (`discover_pending_files`, `pipeline.py:203`); a changed-but-already-embedded
  workbook surfaces via the stale alert and re-embeds through `carta embed <file>`
  — exactly the same lifecycle existing `.pdf`/`.md` files have today, no better
  and no worse.
- Embedded chunks carry `file_path = <spreadsheet rel path>`, so **search hits cite
  the workbook**, not the derived file.
- The companion `.md` under `.carta/companions/<rel>.md` is a transparency /
  inspection artifact ("what did Carta actually index from my workbook?") whose
  content is exactly what was embedded. It is freely deletable and regenerated;
  it is **not** independently discovered or embedded.
- Companion filename rule: **append** `.md` to the full source filename
  (`battery.xlsx` → `battery.xlsx.md`), never `with_suffix()` — otherwise
  `data.csv` and `data.xlsx` in the same directory would collide on one
  companion file. (The sidecar layer has exactly this collision today — see
  "Sidecar path collision" below.)

### Unit 2 — Human gotchas via `remember --about`

Durable, hand-written notes already have a home: `carta remember`
(`carta/memory/capture.py`) writes a frontmatter markdown file into
`docs/quirks/` or `docs/notes/` and embeds it. We add one optional association:

- New optional `--about <path>` (CLI) / `about` param (MCP `carta_remember`) that
  records the target file in the note's frontmatter as `about: <repo-rel path>`.
  Plumbing: `carta_remember` → `_remember` → `capture_note` (`mcp/server.py:501`,
  `capture.py:32`); the frontmatter dict is built at `capture.py:70`.
- Normalize `about` to a repo-relative path; if the target doesn't exist, warn to
  stderr but still capture the note (the association is metadata, not a foreign-key
  constraint — the note may describe a file that is renamed or not yet created).
- The note stays human-facing in `docs/`, durable, and already searchable.

**The two units are not physically merged.** Both carry the parent workbook's path
in their metadata, both are in the index, so a search for `TMS` surfaces whichever
is relevant — the extracted frame names *and* any "x marks the spot" quirk — and
both cite the workbook. The index does the merging; no file-region convention is
needed.

## Architecture & integration points

| Concern | Change | Location |
|---|---|---|
| Discovery | Add `.xlsx`, `.csv` to the supported set. Also picked up automatically by `_heal_sidecar_current_paths` (`pipeline.py:1183`), which iterates the same constant | `pipeline.py:73` `_SUPPORTED_EXTENSIONS` |
| Extraction dispatch | New branch: `.xlsx`/`.csv` → `extract_spreadsheet_text()`. Compare suffixes **lowercased** — the existing branches compare raw `file_path.suffix`, so an uppercase `.MD` file (which discovery *does* yield, case-insensitively) currently falls through to the PDF extractor; don't replicate that latent bug for spreadsheets, and fix the `.md`/`.pdf` comparisons while touching this dispatch | `pipeline.py:347-367` |
| Extractor (new) | `extract_spreadsheet_text(path) -> (pages, meta)` | new module `carta/embed/tabular.py` |
| Companion render | Write `.carta/companions/<rel>.md` (append `.md`, keep source extension); return rendered text as `pages` | `carta/embed/tabular.py` |
| Citation metadata | Chunks already inherit `file_path` = source path → workbook. Add `derived: "spreadsheet"` + `companion_path` marker (payload is free-form; no schema change) | `pipeline.py:387` metadata dict |
| Hashing | `.xlsx` → raw bytes (like PDF); `.csv` → LF-normalized (it is text). See "Lifecycle hashing fix" below | `carta/embed/lifecycle.py:20` `compute_file_hash` |
| Sidecar stub | `file_type` gains `"spreadsheet"` — current code is a binary `"markdown" if .md else "pdf"`, so without this a workbook sidecar would claim `file_type: pdf`. Lowercase the suffix here too. **Must land in the same commit as the `_SUPPORTED_EXTENSIONS` change** — auto-induction (`pipeline.py:1425`) writes stubs for every discovered file | `induct.py:86` |
| Sidecar naming | Extension-preserving sidecar filenames for the new types — see "Sidecar path collision" below | `induct.py:25` `sidecar_path` |
| Discovery exclusion | Already doubly guarded (verified): embed discovery sweeps only `docs_root` (`docs/` — `pipeline.py:1423`), and `.carta/` is in default `excluded_paths` (`config.py:31`). Caveat: `_iter_inductable_files` itself never consults `excluded_paths`, so a project with `docs_root: "."` **would** sweep `.carta/companions/`. Pin with a regression test; if trivial, add a `.carta/` guard to the sweep | `_iter_inductable_files`, `config.py` DEFAULTS |
| Zero-text status | A workbook with no text-bearing cells → a `no_text_content` status, not the harsher `extraction_failed`. This is a **new status in a closed vocabulary** — see "`no_text_content` ripple" below | `pipeline.py:405` + status consumers |
| Scanner | `carta scan`'s induction check has its **own** extension set — `_EMBED_EXTENSIONS` (`scanner.py:641`, currently `.pdf` + audio) — which the spec previously missed. Add `.xlsx`/`.csv` so un-inducted spreadsheets are flagged. Note its scan scope is only `reference_docs_path` + audio dirs (`_get_embed_scan_dirs`), narrower than embed discovery; acceptable for v1, document it | `scanner.py:641` |
| Human notes | `about` frontmatter field + `--about` flag / MCP param | `capture.py:32` (signature) / `:70` (frontmatter), CLI, `mcp/server.py:501` |

The `pages` contract consumed by `chunk_text` is honored: **one logical page per
sheet** (a `.csv` is a single page). The concrete shape (verified against
`parse.py:196`) is `{"page": int, "text": str, "headings": list[str]}` —
`chunk_text` propagates `headings[0]` into each chunk's `section_heading`, so
putting the **sheet name** in `headings` makes search hits display
`battery.xlsx · CAN_Signals` style anchors for free, exactly like markdown
sections do today.

### Sidecar path collision (must fix alongside this feature)

`sidecar_path()` (`induct.py:25`) maps a source file to its sidecar with
`rel.with_suffix(".embed-meta.yaml")` — it **strips the source extension**. So
`docs/data.csv` and `docs/data.md` map to the *same* sidecar file. Auto-induction
(`pipeline.py:1427`) checks `if not sc_path.exists()`, so whichever file is seen
second is **silently never inducted** — no error, no embed, invisible to search.
Today this requires the rare `foo.md` + `foo.pdf` pair; adding `.csv`/`.xlsx`
makes same-stem pairs likely (`data.csv` exported from a doc named `data.md`,
etc.). Same family as issue #89 (slug collisions from dropping the extension).

**Decision (v1):** extension-preserving sidecar names for the *new* types only —
`data.csv` → `.carta/sidecars/docs/data.csv.embed-meta.yaml` (append, don't
replace). `.md`/`.pdf` keep the current mapping, so existing sidecar state is
untouched (zero-regression constraint) and no migration is needed. The two
consumers that recompute this mapping must stay symmetric:
`iter_canonical_sidecars` (`induct.py:167`, `expected_rel` check) and
`_heal_sidecar_current_paths` (`pipeline.py:1180`, stem→source inference).
Unifying `.md`/`.pdf` onto extension-preserving names too is the eventual fix for
the whole class, but that is a migration and belongs to #89's scope, not this
feature.

### `no_text_content` ripple (closed status vocabulary)

Sidecar `status` is a closed vocabulary with several consumers that bucket
unknown values into "other" — adding a status without registering it is exactly
the issue-#88 bug class. Verified touchpoints:

- `carta/status.py:20` `_CORPUS_STATUSES` + the bucket dict at `:87` + the
  summary renderer at `:199` — register `no_text_content` so `carta status`
  reports it instead of counting it as "other".
- `run_embed` summary dict + per-file counting branch (`pipeline.py:1379`,
  `:1567`) and the end-of-run CLI warning (`cli.py:253-260`) — the current
  message says "scanned PDFs? OCR may be required", which is wrong for a
  numeric-only workbook; give `no_text_content` its own count and one-liner.
- `discover_pending_files` (`pipeline.py:203`) — **not** re-pickable, mirroring
  `extraction_failed`: a numeric-only workbook is a healthy terminal state, not
  a retry candidate. It re-embeds when its content hash changes, like any file.
- `carta embed --repair` (`repair.py:73`) re-embeds `extraction_failed` sidecars
  — `no_text_content` is deliberately **excluded** (nothing is damaged).
- `carta audit` integrity scan: no change needed — the count-mismatch check
  (`integrity.py:157`) only fires for `status == "embedded"`/stuck-stale, so a
  `no_text_content` sidecar with zero points is not flagged. Pin with a test.

### Lifecycle hashing fix (small, in-scope)

`compute_file_hash` (`lifecycle.py:20`) CRLF→LF-normalizes **every non-`.pdf`
file** before hashing. For a binary `.xlsx` (a zip container) the result is still
deterministic, but it is semantically wrong: two distinct workbooks differing
only in `\r\n`/`\n` byte sequences inside the container would hash identically,
and every hash of a binary pays a pointless full-buffer rewrite. This function is
on the hot path — `discover_stale_files` calls it for every sidecar'd source on
each run. Fix: dispatch on a small `_BINARY_SUFFIXES = {".pdf", ".xlsx"}` set →
raw bytes; `.csv` stays LF-normalized with the other text formats (correct — CRLF
churn from Windows checkouts should not trigger re-embeds). Existing `.md`/`.pdf`
hashes are unaffected, so no sidecar state regresses.

## Extraction heuristic (the core new logic)

Deterministic, per sheet:

1. **Header row**: first non-empty row is treated as headers.
2. **Column classification** by sampling values down each column:
   - Predominantly numeric → **numeric column**: emit only the header name and a
     compact range summary (e.g. `MsgID (numeric 0x100–0x7FF)`), never the cell
     values.
   - Predominantly string → **text column**: emit the distinct values (these are
     the frame names — the BM25 targets). Note: sparse vectors come from
     fastembed `Qdrant/bm25` (`sparse.py:32`), which applies its own
     tokenization — whether `TMS_CoolantTemp` survives as one token or splits at
     the underscore determines which query shapes match. Don't assume; pin with
     a retrieval test covering both the exact identifier and the `TMS` fragment.
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
- LFS-pointer workbooks (likely for binary `.xlsx` in real repos) → already
  skipped by the batch run's `is_lfs_pointer` guard (`pipeline.py:1462`); no new
  handling needed. `run_embed_file` (the targeted path) has no such guard today —
  out of scope, but worth an issue if spreadsheet corpora start using LFS.

## Testing (TDD)

Failing-test-first, per feature:

- Extractor unit tests (`carta/embed/tests/`): numeric-only sheet → stub +
  `no_text_content`; mixed sheet → frame names present, numeric cell values absent;
  notes column fully preserved; multi-sheet → one page per sheet with the sheet
  name in `headings` (→ `section_heading` on chunks); CSV → single page;
  hex-formatted numeric columns summarized as ranges; uppercase `.XLSX`/`.CSV`
  dispatch correctly.
- Pipeline integration: `.xlsx`/`.csv` discovered and embedded; chunk
  `metadata.file_path` equals the workbook path; companion `.md` written under
  `.carta/companions/` and **not** double-discovered by the `.md` sweep (including
  the `docs_root: "."` configuration).
- Sidecar naming: `data.csv` and `data.md` in one directory produce two distinct
  sidecars, both inducted and embedded; `iter_canonical_sidecars` accepts the
  extension-preserving name; `_heal_sidecar_current_paths` resolves it.
- Status vocabulary: `carta status` buckets `no_text_content` by name (not
  "other" — the #88 regression class); run summary and CLI message count it;
  `discover_pending_files` and `--repair` do **not** re-pick it; `carta audit`
  does not flag a `no_text_content` sidecar with zero points.
- Change detection: editing the workbook bumps the sidecar generation and
  regenerates the companion note; unchanged workbook is skipped (mtime and hash
  fast-paths).
- Hashing: `.xlsx` hashed as raw bytes; `.csv` LF-normalized; existing `.md`
  hashes unchanged by the refactor (byte-for-byte identical digests).
- Retrieval: BM25 hit for `TMS_CoolantTemp` (exact) and `TMS` (fragment) against
  an embedded fixture workbook — pins the tokenizer behavior the design leans on.
- `remember --about`: frontmatter carries `about`; MCP param plumbs through;
  nonexistent target warns but captures.
- Regression: existing `.pdf`/`.md` extraction/sidecar tests stay green.

## Eval

Retrieval-quality changes are validated against the ET-embed eval corpus (per
CLAUDE.md). New workbook content only *adds* documents, but we still run the eval to
confirm no regression on the existing 62-question set, and — tying into the #19
eval-growth rescope — add at least one spreadsheet-sourced retrieval case
(e.g. "what is the scaling on `TMS_CoolantTemp`?") so this path is guarded going
forward.

## Rollout / scope

Single implementation plan. Order: (1) extractor module + tests, (2) sidecar
naming (extension-preserving for new types) + lifecycle hashing fix — both are
prerequisites for (3), (3) pipeline discovery/dispatch/companion-write wiring +
status vocabulary registration + scanner `_EMBED_EXTENSIONS`, (4) `remember
--about`, (5) eval case + docs (CLAUDE.md Carta-surface table, README command
notes). No migration — existing collections and sidecars are untouched; the
feature only adds new source files. Note the same-commit constraint:
`_SUPPORTED_EXTENSIONS` must not gain `.xlsx`/`.csv` before `induct.py`'s
`file_type` and `sidecar_path` changes land, or auto-induction writes wrong
(`file_type: pdf`) and collision-prone stubs.

## Future (out of scope, noted)

- `carta focus` for workbooks (per-sheet outline; table cells as images).
- LLM-assisted column semantics for messy sheets where header heuristics fail.
- Additional formats (`.tsv`, `.ods`, `.docx`).
- Surfacing "human notes about this file" inline in the companion rendering (a
  read-time join on the `about` metadata, not a physical merge).
- Unify `.md`/`.pdf` sidecar names onto the extension-preserving scheme
  (migration; belongs with issue #89's slug-collision work).
- LFS-pointer guard in `run_embed_file` (targeted embed path).

## Amendments — 2026-07-02 code audit

Every code claim in the original draft was verified against the tree; all cited
line numbers were accurate (`pipeline.py:73/347/387/405`, `lifecycle.py`
CRLF-normalization, `induct.py:86`, `capture.py:32`). The audit added:

1. **Sidecar path collision** (new section): `sidecar_path()` strips the source
   extension, so same-stem files silently shadow each other's sidecars — made
   worse by adding new extensions. Resolved with extension-preserving names for
   new types only; no migration.
2. **`no_text_content` ripple** (new section): the status vocabulary is closed
   with five consumer sites (`status.py`, run summary, CLI message, repair,
   pending-discovery); registering the new status everywhere is required or it
   lands in "other" buckets (issue-#88 class). Audit/integrity needs no change
   (verified: mismatch check fires only on `embedded`).
3. **Scanner integration** (new table row): `scanner.py:641` `_EMBED_EXTENSIONS`
   is a second, independent extension list the draft missed.
4. **Suffix-casing**: extraction dispatch compares raw suffixes while discovery
   lowercases — uppercase `.MD` already misroutes to the PDF extractor; the new
   dispatch must lowercase (and fixes the existing comparisons in passing).
5. **Discovery exclusion downgraded to verification**: `.carta/companions/` is
   already doubly excluded by default (docs_root scope + `excluded_paths`);
   remaining exposure is only the `docs_root: "."` configuration.
6. **Change-detection trigger precision**: regeneration rides the existing
   hash/generation machinery, but the batch run does not re-pick
   changed-but-embedded files — same trigger semantics as `.pdf`/`.md` today.
7. **BM25 tokenization pinned by test** rather than assumed for
   underscore-identifier queries.
