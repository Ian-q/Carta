# Demand-Driven Deep Scanning for Visual Documents

**Date:** 2026-07-29
**Status:** Approved (brainstormed with Ian, session "carta updates")
**Motivating failure:** the MSD case study, below.

## Case study — why this exists

ET-embed's CTS battery schematic (`docs/reference/suppliers/CTS/H-FL-400150-1_电气原理图V1.3_20260105 2.pdf`) draws an `MSD 300A` element mid-string between two battery module groups. On 2026-07-29 an agent needed exactly that fact, and every retrieval layer missed:

- The PDF is vector line art with outline text — `page.get_text()` returns ~0 chars, so a text-layer grep concluded (wrongly) that no MSD element exists.
- Carta had the file `status: embedded` but as **1 chunk** (the "CTS" title block), `visual_pages: 0`, `spec_summary: null`.
- Three stacked causes:
  1. The file's last two sidecar generations were written by **repair/restore** runs, which skip page classification and never write `visual_pending` (`carta/embed/pipeline.py:375-389` fail-closed path) — the file is invisible to the visual drain.
  2. The project-wide **visual queue backlog** (1,899 pages / 141 files at the 2026-07-11 full run) was drained once for a few minutes and never finished.
  3. Even a completed drain would have OCR'd scattered labels; "does the MSD split the pack mid-string?" is a **topology** question that a bag of transcribed labels does not answer.

The classifier itself was correct: near-zero text → `FLATTENED` → OCR/vision route (`carta/vision/classifier.py:88`).

## Goals

1. Repair-path embeds must re-queue image-heavy pages (bug fix).
2. Agents can **flag** a document mid-session as high-priority for deep scanning, via CLI, recorded in the sidecar.
3. Flagged files get a **local deep tier**: high-DPI, tiled, topology-aware vision extraction (automatic, next drain).
4. Flagged files can get a **Claude tier**: session-authored structured extraction ("enrichment"), stored per project config, embedded as text, staleness-tracked against the source hash.
5. The visual drain processes flagged files first, then configured triage paths, then the rest — resumable.
6. Validate end-to-end on ET-embed's five known-dark supplier drawings.

## Non-goals (deliberate)

- **ColPali re-enable.** The dense-only decision stands until an eval says otherwise. (Note: `_mark_or_collect_visual_pages` currently reuses `colpali_scoped_paths` to gate the *whole* second-pass queue — see Component 1 cleanup.)
- **Homelab offload.** `embed.ollama_url` is already config; pointing vision at another host later is a repoint, not code.
- **Auto-flagging on retrieval misses.** Future work; this spec only builds the manual/agent flag.

## Design

### Component 1 — repair path re-queues visual pages

Repair/restore embed paths run `PageAnalyzer` classification (zero model calls — pure PyMuPDF metadata) and write `visual_pending` via `_mark_or_collect_visual_pages`, identically to a normal embed. The fail-closed fallback at `pipeline.py:379-389` stays for genuine classification *exceptions*, but repair no longer skips classification by construction.

**Cleanup in the same component:** `_mark_or_collect_visual_pages` (`pipeline.py:52-78`) must stop consulting `colpali_scoped_paths` when queueing OCR/VLM pages. ColPali scoping gates ColPali; it must not silently gate the OCR/vision drain (this trap contributed to the June–July dark corpus). If drain-scope control is wanted, it comes from the new triage config below, visibly.

### Component 2 — sidecar flag + CLI

New sidecar fields (absent = defaults):

```yaml
priority: high            # only ever "high"; absent means normal
deep_scan: requested      # requested | done
deep_scan_reason: "MSD topology question missed 2026-07-29"
deep_scan_requested_at: "2026-07-29T08:10:00+00:00"
```

CLI (agents never hand-edit sidecar YAML):

- `carta flag <path> --reason "<one line>"` — sets all four fields (`deep_scan: requested`).
- `carta flag` (no args) — lists flagged files with reasons and deep-scan state.
- `carta flag <path> --clear` — removes the flag fields.
- `deep_scan: done` is set by the pipeline (after the deep local pass) or by `carta embed <enrichment-file>` completing enrichment ingestion — not by hand.

`carta status` gains one line: `flagged N (M awaiting deep scan) · enrichment stale: K`.

### Component 3 — local deep tier (automatic)

Trigger: the drain reaches a file with `deep_scan: requested`, **or** a page matches the *vector-CAD signature*: `page_class == FLATTENED` **and** zero raster images **and** `len(page.get_drawings()) >= vector_min_paths`.

Treatment (config under `embed.deep_scan`):

```yaml
deep_scan:
  dpi: 300              # vs the standard flattened render
  tile_px: 1280         # max tile edge sent to the VLM
  tile_overlap: 0.15
  vector_min_paths: 50
```

Each page renders at `dpi`; sheets larger than `tile_px` are tiled with `tile_overlap`. Every tile runs **two prompts**:

1. The existing transcription prompt (`carta/vision/router.py:121`).
2. A structure prompt: *"Describe what this technical drawing shows: name each component, state what connects to what, and note what sits between which elements. Transcribe labels verbatim, including non-English text. Do not invent details; omit anything unreadable."*

Chunks carry `page`, `tile`, and `extraction: transcription|structure` metadata. This is a best-effort 8B-model tier and is documented as such; the Claude tier exists because of that limit.

### Component 4 — Claude tier (session enrichment)

When an agent flags a file (or immediately when it cares), it authors a structured extraction by reading the PDF pages natively: per sheet — title block, element inventory with verbatim labels (+ translation for non-English), connectivity/topology statements, key values, page anchors.

**Storage — mechanism is Carta's, location is per-project config:**

```yaml
embed:
  enrichment:
    repo_visible: false        # Carta default: companion-internal
    suffix: ".extraction.md"
```

- `repo_visible: false` → `.carta/companions/<mirror-path>/<stem>.extraction.md` (extends the existing spreadsheet-companion mechanism).
- `repo_visible: true` → `<source-dir>/<stem>.extraction.md` in the repo tree, committed and reviewed like any derived artifact. **ET-embed sets this true.**
- Exactly one canonical location per project; no dual-store sync.

Sidecar records `enrichment_path` and `enrichment_source_hash`. On embed, the enrichment file is chunked as normal text with payload `enriches: <source rel path>`, so results cite both the enrichment and the source PDF. If the source `file_hash` changes, `carta status` and `carta audit` report the enrichment **stale** (the enrichment is not deleted — it may still be mostly right — it is flagged for re-verification).

### Component 5 — drain ordering + ops

Queue order at drain time:

1. Files with `priority: high` (oldest `deep_scan_requested_at` first).
2. Files matching `embed.visual_triage_paths` (new config, list of path prefixes; ET-embed sets `["docs/reference/suppliers/"]`).
3. Everything else, FIFO.

Drain remains resumable via the existing checkpoint (`carta/vision/router.py:31-118`). Expected ops pattern: overnight Mac runs until the 1,899-page backlog clears.

### Component 6 — ET-embed rollout (end-to-end validation)

1. Flag the five known-dark supplier drawings: CTS schematic, CTS pack assembly drawing, GG e-axle drawing (`1693505 A1(2).pdf`), PCU outline (`TCKH-1684A`), TZ220 MCU manual.
2. Claude authors the CTS schematic extraction first (already read this session — MSD 300A mid-string between the two module groups, relay/precharge/fuse elements, BMS sense taps).
3. Set ET-embed config: `enrichment.repo_visible: true`, `visual_triage_paths: ["docs/reference/suppliers/"]`.
4. Embed; verify `carta search "MSD mid-string"` surfaces the schematic via its enrichment.
5. Correct the wrong "mid-string is unconfirmed" caveat on ET-embed issue #276.

## Error handling

- Drain failures keep the existing warn-and-continue per page; a tile failure degrades to remaining tiles, never aborts the file.
- `carta flag` validates the path resolves to a tracked source under `docs_root`; unknown path → error, no sidecar created.
- Enrichment with a missing/renamed source: audit reports orphaned enrichment.
- Repair classification exceptions retain today's fail-closed text-only behavior (with the warning), but now log that visual queueing was skipped.

## Testing

- **Unit:** repair-path re-queue writes `visual_pending`; `_mark_or_collect_visual_pages` ignores `colpali_scoped_paths`; flag CLI set/list/clear round-trip; drain ordering (flagged → triage → FIFO); vector-CAD signature classification; tiling geometry (coverage + overlap); enrichment staleness on source-hash change.
- **Integration:** fixture vector-PDF (synthetic CAD sheet with an outline-text label) goes dark under the old path and retrievable under the new one.
- **Eval:** add retrieval cases to `carta eval` — "MSD mid-string", "e-axle bolt circle" — recall must hit the schematic/enrichment. Regression-guards this failure class.

## Implementation order

1. Component 1 (bug fix + scope cleanup) — smallest, unblocks everything.
2. Component 2 (flag + CLI + status).
3. Component 5 (drain ordering) — trivial once 2 exists.
4. Component 4 (enrichment storage + staleness) — unblocks the ET-embed payoff without waiting on vision work.
5. Component 3 (deep local tier: DPI/tiling/structure prompt).
6. Component 6 (ET-embed rollout + backlog drain nights).
