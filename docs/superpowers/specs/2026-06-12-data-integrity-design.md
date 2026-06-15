---
id: 2026-06-12-data-integrity-design
title: "Carta v0.11.0 — Data Integrity: point-ID collisions, empty-chunk fail-open, corpus repair"
status: shipped
related:
  - 2026-06-12-data-integrity
date: 2026-06-12
---

# Carta v0.11.0 — Data Integrity: point-ID collisions, empty-chunk fail-open, corpus repair

**Date:** 2026-06-12
**Status:** Approved (design review with Ian, 2026-06-12)
**Origin:** Issue #19 depth-check investigation on the ET-embed 62-query eval (8 misses).

## Background: what the investigation found

The 8 remaining eval misses were assumed to be chunking-bound. A per-query depth
check (dense leg, BM25 leg, fused pool at standard and 300-deep settings, plus
the real cross-collection merged pool) showed none of them is primarily a
chunk-size problem. Root causes, with evidence:

| Miss | Evidence | Root cause |
|---|---|---|
| ci/README | Only chunks 8–9 of 10 survive in Qdrant; chunks 0–7 overwritten | **A: point-ID collision** |
| US-11965795 patent | 32 points, all `text: ""`, byte-identical vectors | **B: empty-chunk fail-open** |
| SAFETY-MCU-MESSAGES | Text-only fused #34; merged pool absent | C: visual pool dilution (v0.12.0) |
| TIMING_ARCHITECTURE | Deep fused #34, outside 40-pool | C + weak first-stage rank (v0.12.0) |
| connector-map | Merged pool #35/40; reranker doesn't promote | C + D (v0.12.0) |
| US10245972 | First-stage #2–3; reranker demotes out of top-5 | D: reranker demotion (v0.12.0) |
| cts-control | Pool #4–7, not promoted | D (v0.12.0) |
| pcb/DESIGN_CHECKLIST | Retrieval ranks `vcu/pcb-design-checklist.md` #1 — the file that actually contains the 120Ω/PESD2CAN content | **Eval expect points at wrong file** |

**Bug A (data loss).** `_point_id()` / `_point_id_versioned()`
(`carta/embed/embed.py:154-167`) hash only `slug:chunk_index[:generation]`, and
slug is the filename stem (`carta/embed/induct.py:31-36`). Same-stem files
(README.md ×4 in ET-embed) overwrite each other's points: 10 points survive
where ~25+ should exist. `_visual_point_id(slug, page_num)`
(`embed.py:312-323`) has the identical bug for the visual collection.

**Bug B (silent fail-open).** PDF extraction that yields no text still flows
into embed+upsert. ET-embed has 43 fully-empty files (~1,400 points, all with
identical embedding-of-empty-string vectors — confirmed cos=1.0) and 24
partially-empty files, including the project's own PCT filing (67/135 chunks
empty). These points are unfindable by dense or BM25 and pollute dense space.

**Bug E (bookkeeping).** `run_embed_file` (`carta/embed/pipeline.py:1146`)
merges `lifecycle_updates` (containing `status: stale`, `stale_as_of`) OVER the
success updates from `_embed_one_file`, so every successfully re-embedded
file's sidecar permanently reads `stale` (169 of 971 ET-embed sidecars).

**Generation leak (related to A).** Old-generation points are stamped stale via
`mark_sidecar_stale` (guarded on `sidecar_id`, which is empty for most legacy
points) but never deleted; search does not filter them. Re-embedded files leave
their previous generation's chunks searchable indefinitely.

## Scope

v0.11.0 fixes A, B, E, the generation leak, adds detection (`carta doctor`) and
repair (`carta embed --repair`), and repairs the ET-embed corpus + eval set.

Out of scope (deferred, tracked as v0.12.0 candidates):
- OCR recovery for scanned PDFs whose extraction yields nothing (Bug B files
  become honestly *flagged*, not findable).
- Visual pool dilution (Bug C) and reranker demotion (Bug D).
- Excluding ET-embed's `_dropped/`/`_marginal/` patent directories.

## Design

### 1. Path-based point IDs (Bug A)

- `_point_id_versioned(rel_path, chunk_index, generation)` →
  `md5("{rel_path}:{chunk_index}:g{generation}")`. Same change to `_point_id`
  (legacy path) and `_visual_point_id` (`md5("{rel_path}:p{page_num}")`).
- ID basis is the repo-relative `file_path` already present in every chunk
  payload. `upsert_chunks` and the visual upsert derive the ID from
  `chunk["file_path"]`, falling back to slug only if file_path is absent
  (defensive; all current callers set it).
- Point IDs are opaque — nothing reads them back — so no consumer changes.
  `carta export`/`import` (`share.py`) must be verified early in implementation
  to confirm it round-trips IDs without assuming the scheme.

### 2. Generation cleanup on re-embed

After a successful, complete upsert for a file, delete every point where
`file_path == <file>` EXCEPT the point IDs just written (`HasIdCondition`
exclusion — Qdrant filtered delete). Generation remains as a payload field
(`doc_generation`) for observability but is no longer the cleanup key.

This ID-set-based approach (`delete_other_points`) subsumes the earlier
generation-arithmetic strategy and closes holes it could not cover:
- **Tail chunks of shrunken files**: a file that loses chunks on re-embed at
  the *same* generation still has its orphaned tail removed, because those
  points are not in the just-written ID set.
- **Legacy slug-keyed duplicates**: old points with different IDs (keyed by
  filename stem instead of file path) are caught by the file_path filter and
  excluded from the keep set, so they are deleted on first re-embed.
- **Bulk-path generation reuse (C1)**: bulk sidecars initialised at
  `generation: 0` caused the first re-embed to reuse generation 1; the old
  `doc_generation != g` filter then spared all prior gen-1 points. The ID-set
  approach is independent of generation arithmetic and is unaffected by this.

`_embed_one_file` also persists `generation` to `sidecar_updates` so that bulk
sidecars correctly record the generation after their first embed.

- The keyword payload index on `file_path` originally planned for
  `ensure_collection` is **deferred** (recorded at final review): filtered
  deletes full-scan, which is acceptable at local corpus sizes; revisit if
  collections grow past ~10⁵ points.

### 3. Empty-chunk guard (Bug B)

In `_embed_one_file` (text path) and the visual path:
- Drop chunks whose text is empty/whitespace before embedding; count drops.
- File yields **zero** non-empty chunks → upsert nothing, set sidecar
  `status: extraction_failed`, print a warning naming the file:
  `"0 extractable characters — skipped (scanned PDF? OCR may be required)"`.
- File yields **some** empty chunks → embed the rest; report
  `"N empty chunks dropped"` (verbose per-file; aggregate in the embed summary).
- `run_embed` summary gains an `extraction_failed` count alongside
  embedded/skipped.

### 4. Sidecar status bookkeeping (Bug E)

`run_embed_file`: on successful embed, the final sidecar state is
`status: embedded` (the codebase's existing canonical healthy value — not
`current` as originally drafted), `stale_as_of: null`; `extraction_failed`
from the embed itself survives the merge. On an embed exception nothing is
written, so the sidecar keeps its prior state and the file stays
re-discoverable. Sidecar-level `stale` is no longer persisted at all — the
stale marker during the re-embed window lives on the old generation's Qdrant
points (`mark_sidecar_stale`), which generation cleanup then removes.

### 5. `carta doctor` corpus-integrity checks (read-only)

New doctor section scanning the project's `_doc` collection plus sidecars.
(`_visual` scanning is deliberately deferred to the visual/OCR follow-up:
repair cannot re-create visual points it purges until a visual re-embed path
exists, so detecting damage there without a fix path would only mislead.)

1. **Slug collisions:** >1 embedded file sharing a slug (these overwrite each
   other under the legacy ID scheme).
2. **Empty-text points:** files with ≥1 empty-text point; report fully-empty
   vs partial counts.
3. **Count mismatches:** sidecar `chunk_count` vs actual point count per
   `file_path`.
4. **Stuck-stale sidecars:** `status: stale` while `file_hash` matches the file
   on disk.

Human-readable summary + `--json`, consistent with existing doctor output.
Doctor only reports; it directs the user to `carta embed --repair`.

### 6. `carta embed --repair`

Runs the same detection, then per affected file:
- Delete all points for that `file_path` (filtered delete, both collections as
  applicable).
- Force re-embed via `run_embed_file(force=True)` through the fixed pipeline
  (new IDs, empty guard, correct status bookkeeping). Note: `force=True` today
  only bypasses the mtime fast-path; the hash short-circuit at
  `pipeline.py:1082` would still skip files whose sidecar hash matches disk
  (true for all 43 empty files). Repair must bypass the hash short-circuit as
  well — `force` is extended to mean "re-embed unconditionally".
- Empty-extraction files therefore end up purged + `extraction_failed`, not
  re-upserted. Stuck-stale sidecars get their status corrected even when no
  re-embed is needed.
- Prints per-file actions and a final summary (repaired / purged / flagged).

### 7. ET-embed corpus repair + eval fix (post-release, not Carta code)

- Run `carta doctor` (verify detection matches the investigation's numbers),
  then `carta embed --repair`: re-embeds the 4 collided READMEs, purges ~1,400
  fully-empty points + ~150 partial-empty chunks, fixes 169 stuck-stale
  sidecars.
- Eval set: change the termination query's `expect` to
  `["vcu/pcb-design-checklist"]`.
- Re-run the 62-query eval (hybrid-only and reranked), update `RESULTS.md`.
  Expected: ci/README and the pcb query recover (+2 hits); US-11965795 remains
  an honest, flagged miss pending OCR work; possible incidental gains from
  removing ~1,500 garbage vectors from dense space.

## Testing

Strict TDD per task. Key cases:
- **Collision regression:** embed two same-stem files; assert distinct point
  IDs and both files fully retrievable by file_path; assert visual page IDs
  likewise distinct.
- **Generation cleanup:** re-embed a changed file; assert old-generation points
  gone, new generation intact; assert a *different* file's points untouched.
- **Empty guard:** all-empty extraction → no upsert, sidecar
  `extraction_failed`, warning printed; partial-empty → only empty chunks
  dropped, drop count reported.
- **Status bookkeeping:** successful re-embed ends `status: embedded`,
  `stale_as_of: null`; an embed exception writes nothing (prior state kept).
- **Doctor checks:** each of the four detections against fixture corpora
  (mocked Qdrant or ephemeral collections, matching existing test patterns).
- **Repair:** integration test covering delete→re-embed and purge+flag paths.
- All 798 existing tests stay green; tests pinning the old ID derivation are
  updated deliberately (the change is the point).

## Release

- Version 0.11.0; CHANGELOG; README docs for doctor integrity checks and
  `embed --repair`.
- Update issue #19 with the investigation findings (chunking hypothesis
  largely disproven); file v0.12.0 issues for Bug C (pool dilution), Bug D
  (reranker demotion), and OCR recovery.
