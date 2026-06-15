---
id: 2026-06-15-ocr-recover-extraction-failed-design
title: OCR recovery for extraction_failed scanned PDFs
status: shipped
related: [2026-06-15-visual-integrity-scanning-design]
date: 2026-06-15
related_issue: Ian-q/Carta#38
---

# OCR recovery for extraction_failed scanned PDFs (#38 part 1)

v0.11.0 stops silently embedding empty chunks: a PDF whose text extraction
yields nothing is flagged `status: extraction_failed` and then **dead-ends** —
nothing ever routes it to OCR, so it stays unfindable (43 such files in the
ET-embed corpus). Part 2 (`_visual` integrity) already shipped (#64).

## Root cause (from code trace)

`_embed_one_file` (`carta/embed/pipeline.py`) flags `extraction_failed` only
when **all** of: zero text chunks, zero inline image chunks, and **no pages
queued** for the two-pass `--visual` drain. A scanned page normally classifies
as `FLATTENED` and gets queued — but when page classification fails (PyMuPDF
error → `_page_classes_from_extraction is None`, fail-closed) or yields no
image-heavy pages, nothing is queued and the textless PDF dead-ends. The OCR
machinery (`run_visual_embed` → `SmartRouter._route_flattened` → glm-ocr) exists
and works; it's just never reached for these files.

## Design — give OCR a chance before flagging

At the `extraction_failed` gate, before flagging a **PDF**, queue **every** page
for the `--visual` drain instead. The drain (unchanged) runs glm-ocr text →
`_doc` (hybrid index) **and** ColPali → `_visual` per page, so the file becomes
findable. `extraction_failed` is reserved for cases OCR genuinely can't help:

- non-PDF files (markdown, etc.) — no OCR path (existing line ~379, unchanged);
- zero-page PDFs (`len(pages) == 0`);
- visual/OCR disabled (`two_pass_visual` off, or `vision_routing == "off"`).

The queued file keeps `status: embedded` with `visual_pending = [1..N]` — the
same healthy two-pass state a normal scanned PDF reaches. Running
`carta embed --visual` then drains it through OCR.

### Why not flag-after-OCR-fails

The drain already embeds each page with ColPali regardless of OCR yield, so a
queued page is never a total loss (it's visually searchable even if glm-ocr
returns no text). A future refinement could downgrade a file to
`extraction_failed` if a drain produces neither OCR text nor ColPali points, but
that requires per-file bookkeeping across the drain loop and is out of scope.

## Implementation

Single change in `_embed_one_file`'s zero-extractable gate: replace the
unconditional `status = extraction_failed` with a branch that force-queues all
pages (`add_pending_pages(sidecar_updates, range(1, len(pages)+1))`) when the
file is an OCR-recoverable PDF, else flags as before.

## Testing (TDD)

- Textless PDF, nothing queued, visual enabled → all pages queued
  (`visual_pending == [1..N]`), status NOT `extraction_failed`.
- Textless PDF with `vision_routing: "off"` (or `two_pass_visual: false`) →
  still `extraction_failed`.
- Empty markdown (non-PDF) → still `extraction_failed` (unchanged).
- PDF with image-heavy pages already queued → unchanged (not flagged).

## Validation

Mock-based TDD here; real-corpus validation is run by the maintainer against
ET-embed (re-embed the 43 `extraction_failed` files, then `carta embed --visual`,
and confirm they become findable).

## Acceptance

A scanned PDF that previously dead-ended at `extraction_failed` is instead
queued for the OCR drain and recovered by `carta embed --visual`;
`extraction_failed` remains only for genuinely unrecoverable inputs.
