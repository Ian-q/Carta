---
id: 2026-06-25-visual-doc-generation-cleanup-design
title: Visual ColPali points — doc_generation stamp + orphan cleanup
status: approved
related:
  - docs/superpowers/specs/2026-06-07-two-pass-visual-embedding-design.md
  - docs/superpowers/specs/2026-06-12-data-integrity-design.md
  - docs/superpowers/specs/2026-06-20-ocr-trust-handling-design.md
related_issue: Ian-q/Carta#78
date: 2026-06-25
---

# Design — Visual ColPali points: doc_generation stamp + orphan cleanup

**Date:** 2026-06-25 · **Status:** approved · **Target:** carta-cc (next patch) · **Closes:** [#78](https://github.com/Ian-q/Carta/issues/78) (points 1, 2, 4 — point 3 spun out as a fast-follow).

## Problem

ColPali/OCR **visual points are not generation-stamped and are never cleaned up on
re-embed.** `upsert_visual_pages` (`carta/embed/embed.py:381`) writes each page's payload
with `doc_type` but no `doc_generation`/lifecycle fields, and the visual lane never calls
`delete_other_points` — the file-scoped sweep the text lane runs after every upsert
(`carta/embed/pipeline.py:695`). So on every re-drain old visual points **orphan instead
of being replaced**, and `carta doctor`'s sidecar-vs-qdrant visual count drifts.

Observed on petsense after a full `carta embed` + `carta embed --visual` sweep
(2026-06-20): `EN_UM_N32WB03x.pdf` had **491 visual points across 475 pages** (mostly
prior-run residue), and every sampled visual point carried `doc_generation: None`.

Two contributing code facts:

1. **No cleanup.** The text lane builds `keep_ids` and calls
   `delete_other_points(client, coll, rel_path, keep_ids)` after a successful upsert
   (`pipeline.py:689-695`); `delete_other_points` (`carta/embed/lifecycle.py:178`) deletes
   every point for `file_path == rel_path` **except** `keep_ids`, *regardless of
   doc_generation*. The visual lane has no equivalent.
2. **Dual-key IDs.** The visual point ID uses `id_key = page.get("file_path") or page["slug"]`
   (`embed.py:434`). A file ever embedded under both keys yields two IDs for the same page —
   a minor duplication source on top of the cleanup gap.

## Root cause

The visual lane was built as append-only. Retrieval still works today only because the #73
search-time `dedupe_results` collapses duplicate `(page N)` hits — a band-aid that hides the
duplicates at query time without fixing storage. As the corpus changes, stale visual content
can persist and surface as wrong answers the dedup can no longer distinguish.

## Goals / Non-goals

**Goals (points 1, 2, 4)**
- **Stamp `doc_generation`** (plus the `stale_as_of`/`superseded_at`/`orphaned_at` lifecycle
  fields) into the visual payload, mirroring `upsert_chunks`.
- **Replace, don't orphan:** re-draining a file's visual pages removes its superseded points —
  legacy slug-keyed points, pre-fix generation-less points, and pages the document no longer
  has — by reusing `delete_other_points`.
- **Stable, single-key point IDs:** normalize `id_key` to always `page["file_path"]` (loud
  stderr fallback to slug, mirroring the text path), with `_visual_point_id(id_key, page_num)`
  kept generation-free so an unchanged page **overwrites in place** on re-drain.
- **No new failure modes:** cleanup stays best-effort (retries, never fails the embed) and runs
  only after a file's pages drain cleanly.

**Non-goals**
- **Point 3 — `carta doctor` count reconciliation** (reconcile the sidecar `visual_done`
  accounting with the `_visual` collection so the count check is meaningful). Spun out as a
  fast-follow issue; this change fixes storage, which is the prerequisite for a meaningful count.
- Bulk cleanup of *existing* orphans in already-drained corpora — handled when a file is next
  (re-)drained, or via `carta embed --repair` (separate path). Not retroactive in this change.
- The text lane, the OCR-text-to-hybrid path, and ColPali embedding/caching — untouched.

## Design

### 1. `upsert_visual_pages` — stamp generation + single-key stable IDs

In the per-page payload build (`embed.py:~428`):
- add `payload["doc_generation"] = page.get("doc_generation", 1)` and
  `payload["stale_as_of"] = None`, `payload["superseded_at"] = None`,
  `payload["orphaned_at"] = None` (mirroring `upsert_chunks` `build_point`,
  `embed.py:258-261`);
- replace `id_key = page.get("file_path") or page["slug"]` with the text-lane pattern:
  `id_key = page.get("file_path")`; if falsy, emit the same loud stderr warning as
  `build_point` (`embed.py:266-272`) and fall back to `page["slug"]`.

`_visual_point_id(id_key, page_num)` is unchanged (stable, generation-free) — so re-upserting
a page reuses its ID and overwrites in place.

### 2. Inline path — cleanup after the per-file batch

In `_embed_visual_pages_colpali` (`pipeline.py:757`), after the `upsert_visual_pages` call
(`~865`): build `keep_ids = [_visual_point_id(rel_path, p["page_num"]) for p in visual_pages]`
and call `delete_other_points(client, f"{project}_visual", rel_path=rel_path, keep_ids=keep_ids)`.
This batch is already per-file, so the sweep is naturally file-scoped.

### 3. Drain path — cleanup per file after its pages finish

`run_visual_embed` (`pipeline.py:1052`) loops **per file** (outer, over `queued` sidecars) then
**per page** (inner). Today `_visual_embed_one_page` upserts a single page with no cleanup.

After a file's inner page-loop completes:
- if **no page of that file failed this run**, derive the full expected key set from the
  authoritative post-drain done list — `keep_ids = [_visual_point_id(rel_path, p) for p in
  sc[VISUAL_DONE_KEY]]` (stable IDs make previously-done pages reconstructable, so they are
  kept, not just this run's pages) — and call `delete_other_points(client, visual_coll,
  rel_path, keep_ids)`;
- if any page failed (left pending), **skip the sweep** for that file this run, mirroring the
  text lane's "clean up only after complete success." The orphans are removed on the next clean
  drain.

`rel_path` comes from the sidecar's `current_path`; the visual collection name is
`f"{cfg['project_name']}_visual"` (as in `upsert_visual_pages`). `keep_ids` is reconstructed at
the call-sites (deterministic IDs), so `upsert_visual_pages`'s return contract is unchanged.

### Error handling

`delete_other_points` already retries `DELETE_MAX_ATTEMPTS` times and prints a warning on
failure without raising (`lifecycle.py:194-205`) — reused verbatim. A failed sweep leaves old
points searchable until the next clean drain or `carta embed --repair`, identical to the text
lane's contract.

## Testing (TDD)

New/extended unit tests in `carta/embed/tests/` (mock `QdrantClient`):
1. `upsert_visual_pages` stamps `doc_generation` (and the lifecycle `None` fields) on the built
   payload; a page with `doc_generation` set carries that value, default `1` otherwise.
2. `id_key` always resolves to `file_path`; a page missing `file_path` warns on stderr and falls
   back to slug (assert the warning + the slug-keyed ID).
3. Drain cleanup: after re-draining a file whose page set shrank, `delete_other_points` is called
   with the file filter and `keep_ids` that **exclude** the removed page's stable ID (assert via
   the recorded `keep_ids`), and previously-`visual_done` pages **are** in `keep_ids`.
4. Partial-failure gate: if a page in the file failed this run, the file's `delete_other_points`
   sweep is **not** called.
5. Inline path: `_embed_visual_pages_colpali` calls `delete_other_points` once per file with the
   batch's `keep_ids`.
6. Regression: existing visual upsert/drain tests stay green; full suite green on 3.10–3.12.

## Rollout / risk

- **Behaviour change is storage-only** — search is unaffected (it already dedups). The first
  re-drain of each file after this ships replaces its orphans; existing orphans in static corpora
  clear on next re-drain or `--repair`.
- **Stable IDs are backward-compatible:** the ID for an unchanged page is the same `file_path`-keyed
  value already written for `file_path`-keyed points; only the legacy slug-keyed duplicates change,
  and those are exactly what the cleanup sweep removes.
- **No retroactive deletion risk:** the sweep is file-scoped and only runs after a clean per-file
  drain, with `keep_ids` derived from the authoritative `visual_done` set — it never deletes a page
  that should exist, and never touches other files.

## Fast-follow (separate issue)

Point 3: reconcile the sidecar `visual_done` count with the `_visual` collection so
`carta doctor` / `carta audit` count checks are meaningful (today they drift on the visual lane).
File as a new issue + a `DOC`/integrity backlog item once this storage fix lands.
