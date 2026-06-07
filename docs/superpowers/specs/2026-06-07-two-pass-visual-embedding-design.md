# Two-Pass Visual Embedding — Design

**Date:** 2026-06-07
**Status:** Approved (brainstorm), pending implementation plan
**Component:** `carta` embed pipeline

## Problem

Carta's single-pass `carta embed` does text extraction, OCR, and (optionally) VLM/ColPali vision all in one synchronous run guarded by a per-file watchdog (`file_timeout_s`, default 600s). On image-heavy PDFs (datasheets) this fails the "embed everything thoroughly" goal:

- The qwen3-vl vision pass is slow and crashed 13× on a real ET-embed run ("model runner unexpectedly stopped — resource limitations"); **96 of ~223 files timed out (>600s) and were skipped.**
- The per-file watchdog *kills* slow files rather than letting them finish — directly hostile to "slow but complete."
- Text and heavy-visual work are coupled, so a few slow datasheets block/abort the whole run.

The user's intent: embed everything thoroughly, locally, even if slow — but make the corpus text-searchable quickly, then grind the heavy visual work in the background.

## Goal

Decouple **"searchable fast"** from **"complete eventually"** via two passes:
1. **Pass-1** (`carta embed`): fast text extraction now; *mark* image-heavy pages for later.
2. **Pass-2** (`carta embed --visual`): slow, resumable background drain that does OCR text **and** ColPali visual embedding for the marked pages.

Non-goals: changing the markdown/text retrieval path (hybrid BM25+dense is done and unaffected); reranking (separate issue #19); changing the dense embedding model.

## Architecture

### Component 1 — Pass-1: fast text + mark (`carta embed`, modified)

For each PDF page, the existing classifier (`carta/vision/classifier.py` `PageAnalyzer`) runs:
- **PURE_TEXT / STRUCTURED_TEXT** → extract text now (PyMuPDF, fast) → text index (dense+sparse), exactly as today.
- **TEXT_WITH_IMAGES / FLATTENED** (image-heavy) → do **not** invoke the heavy VLM/ColPali. Instead:
  - extract whatever quick PyMuPDF text the page has (so the page is not totally absent from text search), and
  - record the page number in the sidecar field `visual_pending` (see Component 2).

Because pass-1 no longer performs slow vision work, the `file_timeout_s` watchdog no longer kills datasheets (text extraction is fast). `vision_routing` continues to govern pass-1 text behavior (`auto`/`ocr`/`vision`/`off`); the new marking happens regardless of routing for image-heavy pages.

**End-of-run summary (the nudge):** after a normal `carta embed`, print e.g.
`Visual queue: 42 page(s) across 18 file(s) await visual embedding. Run \`carta embed --visual\` to process them.`
This is informational only — pass-2 is never auto-started.

### Component 2 — The queue: sidecar state (no new store)

Reuse Carta's existing per-file sidecar lifecycle. Each sidecar gains:
- `visual_pending: [int]` — page numbers awaiting visual embedding.
- `visual_done: [int]` — page numbers already visual-embedded.

The drainer discovers work by scanning sidecars for non-empty `visual_pending` (mirrors `discover_pending_files`/`discover_stale_files`). Per-page state gives **per-page checkpointing** → the visual pass is fully resumable and interrupt-safe.

### Component 3 — Pass-2: slow drain (`carta embed --visual`)

New CLI subflag/mode. Steps:
1. Discover all sidecars with non-empty `visual_pending`.
2. Process pages **one at a time** (slow, throttle-aware via `vision_workers`/`embedding_workers`), with a **generous/disabled per-file timeout** (its own config, e.g. `visual_timeout_s`, default high) so multi-page datasheets finish.
3. Per page:
   a. **OCR text** (glm-ocr) → hybrid text index (dense+sparse), same point-ID/slug scheme.
   b. **ColPali** page-image embedding → the project `_visual` collection (multivector, MaxSim).
4. On success, move the page `visual_pending → visual_done` in the sidecar (checkpoint).
5. On model crash/timeout for a page: **log and leave the page in `visual_pending`** (retried on the next `--visual` run) — never silently drop.

Scoped by the existing `colpali_scoped_paths` (#17), so the visual pass only touches configured dirs (e.g. `docs/reference/datasheets/`).

### Component 4 — ColPali / torch enablement

- `[visual]` remains an **optional** extra (torch + transformers).
- `carta embed --visual` runs a **preflight import check**; if torch/transformers/ColPali aren't importable, it prints clear install guidance and exits cleanly (no crash).
- **Open implementation decision (resolve at build time):** install torch in Carta's own interpreter if wheels exist for it, **else** use a dedicated Python 3.12 venv invoked as a subprocess for the visual pass. (Carta's pipx venv is currently Python 3.14, where torch wheels may be unavailable.) The spec does not pre-commit; the plan will check `pip index`/wheel availability and choose.
- ColPali model: `vidore/colqwen2-v1.0-hf` (native transformers, no PEFT — confirmed loadable via `ColQwen2ForRetrieval`), `colpali_device: mps` on Apple Silicon. (`colqwen2.5` requires colpali-engine/PEFT — out of scope.)

### Component 5 — Config & error handling

- Reuse existing `colpali_*` keys and `vision_routing`.
- Add `embed.visual_timeout_s` (per-page/file timeout for pass-2; default generous, e.g. 3600 or 0=unbounded).
- Document `OLLAMA_MAX_LOADED_MODELS` guidance (avoid glm-ocr + ColPali co-residency memory crashes observed on the real run).
- Errors are per-page and non-fatal: a failing page stays queued; the drain continues.

### Component 6 — Testing

Unit tests with mocked models:
- Classifier → `visual_pending` marking (image-heavy pages queued; text pages embedded now).
- Queue discovery (sidecars with `visual_pending` found; empty ones skipped).
- Drainer per-page checkpoint (page moves pending→done; a simulated crash leaves it pending; resume picks it up).
- Preflight gating when `[visual]` absent (clean message, no crash).
- End-of-pass summary counts.

## Data flow

```
carta embed (pass-1)
  PDF page --classifier--> PURE/STRUCTURED_TEXT --> text extract --> text index (now)
                       \-> TEXT_WITH_IMAGES/FLATTENED --> quick text + mark sidecar.visual_pending[]
  (end) --> summary: "N pages queued; run carta embed --visual"

carta embed --visual (pass-2, slow, resumable)
  scan sidecars for visual_pending[]
    per page: glm-ocr text --> text index ;  ColPali image --> _visual collection
              on success: visual_pending -> visual_done   (checkpoint)
              on failure: leave in visual_pending (retry next run)
```

## Scope

One coherent spec; a multi-task implementation plan: (1) sidecar schema (`visual_pending`/`visual_done`) + pass-1 marking, (2) end-of-pass summary, (3) `carta embed --visual` drainer with per-page checkpoint, (4) `[visual]`/ColPali enablement + preflight, (5) config (`visual_timeout_s`) + docs. Builds on shipped features: hybrid retrieval (#14), colpali scoping + vision_routing (#17).
