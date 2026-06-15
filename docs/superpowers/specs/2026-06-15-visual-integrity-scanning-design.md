---
id: 2026-06-15-visual-integrity-scanning-design
title: _visual collection integrity scanning + repair
status: shipped
related: []
date: 2026-06-15
related_issue: Ian-q/Carta#38
---

# `_visual` collection integrity scanning + repair (#38 part 2)

`scan_corpus_integrity` and `embed --repair` only operate on the `_doc`
collection. The `_visual` collection (ColPali page embeddings produced by the
two-pass `carta embed --visual` drain) has no integrity coverage — count drift
between a sidecar's `visual_done` pages and the actual `_visual` points, and
orphaned visual points for deleted sources, go undetected. This is #38 part 2;
part 1 (OCR-recovery of `extraction_failed` files) is a separate follow-up.

## Background

- `_visual` points live in `collection_name(cfg, "visual")`, keyed by a
  deterministic, idempotent id `md5(file_path:visual:page_num)` — **no
  generation**, so a re-drain overwrites in place.
- Each point payload carries `file_path` and `page_num`.
- Sidecars track visual state in `visual_pending` / `visual_done` (1-indexed
  page lists). `len(visual_done)` is the count of pages already visual-embedded.
- v0.11.0 recorded decision: **repair must not purge what it cannot re-create.**
  ColPali embeddings are not deterministically reproducible, so repair must
  *re-queue for re-drain*, never blind-delete (except when the source is gone).

## Design

### Scanning — extend `scan_corpus_integrity` (`carta/embed/integrity.py`)

After the existing `_doc` scan, also scan the `_visual` collection:

- Scroll `collection_name(cfg, "visual")` (if it exists), tallying point count
  per `file_path`.
- **`visual_count_mismatches`** `{rel: {"sidecar": len(visual_done), "qdrant": n}}`
  — for each canonical sidecar whose **source exists**, where the recorded
  `visual_done` count differs from the `_visual` point count. (Both-zero is not
  a mismatch.)
- **`orphaned_visual_files`** `[rel]` — `file_path`s present in `_visual` whose
  source file no longer exists on disk (purgeable).

These are returned as new report keys. They are kept **separate** from
`affected_files` (which carries `_doc` re-embed semantics); `_visual` is repaired
on its own track.

### Repair — extend `run_repair` (`carta/embed/repair.py`)

After the `_doc` repair loop:

- **`orphaned_visual_files`** (source gone) → delete that file's `_visual`
  points. Safe: nothing to re-create. Count `visual_purged`.
- **`visual_count_mismatches`** (source exists) → **re-queue, never delete**:
  reset the sidecar so every visual page (`visual_done ∪ visual_pending`) becomes
  `visual_pending` and `visual_done = []`, then tell the user to run
  `carta embed --visual` to re-drain (idempotent ids overwrite in place). Count
  `visual_requeued`.

New summary keys: `visual_purged`, `visual_requeued`.

## Testing (TDD)

- Scan: count mismatch detected (`visual_done` vs `_visual`); clean when counts
  agree; orphaned `_visual` points (source gone) reported; empty/safe when no
  `_visual` collection.
- Repair: orphaned visual → points purged; mismatch (source exists) → sidecar
  re-queued (`visual_pending` repopulated, `visual_done` cleared), points NOT
  deleted.
- Refactor the `test_integrity.py` client helper to dispatch by collection so
  `_doc`-only tests are unaffected by the new `_visual` scan.

## Acceptance

- `carta doctor` / `embed --repair` surface `_visual` count drift and orphaned
  visual points.
- Repair re-queues mismatched files for re-drain (never destroying
  un-recreatable embeddings) and purges only truly-orphaned visual points.

## Out of scope

- OCR-recovery of `extraction_failed` files (#38 part 1 — follow-up).
- `_visual` points that exist for a file with no sidecar but a present source
  (rare; not addressed here).
