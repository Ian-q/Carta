# Search result de-duplication — design

- **Date:** 2026-06-17
- **Status:** approved (pending spec review)
- **Issue:** follow-up from the #19 first-stage-recall investigation
- **Scope owner:** Ian

## Context

The #19 investigation (improve first-stage recall) ran a per-lane depth check and a
full-pipeline simulation on the rebuilt 0.952 ET-embed corpus. The conclusion
overturned the premise: **first-stage recall is healthy** — the gold docs are
well-covered and well-embedded. The three residual eval "misses" turned out to be
three unrelated causes, only one of which is a retrieval bug:

| Miss | Real cause | Disposition |
|------|------------|-------------|
| `cts-control` (RJ45 pinout) | Duplicate chunks + duplicate visual pages crowd the returned top-5 | **This spec** |
| `FSM_GAIN_SCHEDULER` | Relevant design/plan docs surface but cannot match the `expect` string (underscore vs hyphen); the matching firmware doc is outranked | Eval-correctness debt — out of scope |
| `US-11965795` (kingpin patent) | Patent PDF has 0 chunks (never OCR'd) | #38 data gap — out of scope |

### The bug

`run_search` (`carta/embed/pipeline.py`) fuses per-collection candidate lists, then
returns `all_results[:top_n]` **with no de-duplication**. When `rerank` and `graph`
are disabled (the default, and what the proactive-recall hook uses), the per-collection
fetch depth is `fetch_limit = top_n`, so the fused pool is shallow. Observed top-8 for
the cts-control query:

```
1,3,5,7. .../specs/2026-04-16-cts-subsystem-vcu-interface-design.md   ← same doc ×4
2,4.     .../Yuchai ... Electric Drive Unit ... .pdf (page 20)         ← same visual page ×2
6,8.     docs/reference/datasheets/tcan1044-q1.pdf (page 26)           ← same visual page ×2
```

Three of the top-5 slots are the same `.md`; the genuinely-relevant `cts-control.md`
is pushed below the cut. This wastes the slot budget on **every** query — the returned
"5 results" routinely cover only 2–3 distinct documents — and it is what buries the
RJ45 gold.

## Goal

Return `top_n` **distinct, non-redundant** documents from `run_search`, recovering the
cts-control/RJ45 miss and improving result quality on every query.

### Non-goals

- First-stage recall / chunking / embedding changes (the investigation showed these are
  already healthy — no re-embed).
- The FSM eval-expectation artifact (separate eval-correctness work).
- Patent OCR coverage / `US-11965795` (#38).
- De-duplicating the underlying corpus or the `_visual` collection's duplicate points
  (a real but separate data-hygiene item — noted under Follow-ups).

## Acceptance bar (strict no-regression)

On the ET-embed 62-query eval (`carta eval .carta/eval/et-embed.yaml -k 5`, the default
rerank-off configuration):

- recall@5 **recovers `cts-control`** (the RJ45 query), and
- **zero regressions** — every query that passes today still passes, and
- recall@5 **≥ 0.952** (current), targeting **0.968** (60/62; the FSM artifact and the
  #38 patent remain out of scope and will still "miss").

Measured by A/B: run the eval with `search.dedupe_results` off (baseline) vs on.

## Mechanism

All changes live in `run_search` plus two small helpers in `carta/embed/pipeline.py`.
The dedup path is gated by a config flag so it is reversible and A/B-measurable; with the
flag **off**, `run_search` behaves exactly as it does today.

### New data flow (flag on, the default)

1. **Fetch a real pool.** Floor the per-collection fetch depth to `_RESULT_POOL_FLOOR`
   (30) even when rerank/graph are off, so dedup has headroom to backfill `top_n`
   *distinct* results. (The `_doc` hybrid query already prefetches 40 per lane, so
   returning ~30 fused candidates adds no extra Qdrant round-trips and negligible cost;
   the `_visual` lane fetches ~30 ColPali hits instead of `top_n`.)
2. **Merge without the visual cap.** Call `_rrf_merge_collections(per_collection,
   pool_limit, visual_max_ratio=1.0)` — pure RRF order, cap deferred to the final stage
   (`1.0` already means "no cap" per its contract).
3. **Graph expansion** (if enabled) — unchanged, operates on the deep ordered pool.
4. **Rerank** (if enabled) — returns the deep reranked pool (`top_n=pool_limit`) rather
   than pre-truncating to `top_n`, so the final stage can dedup + cap + truncate
   uniformly.
5. **De-duplicate by source** — `_dedupe_by_source(all_results)` keeps the first
   (best-ranked) occurrence of each distinct `source` string, preserving order.
6. **Apply the visual cap relative to `top_n`** — `_apply_visual_cap(all_results, top_n,
   visual_max_ratio)` caps visual hits at `round(visual_max_ratio * top_n)` (= 1 at the
   0.2 default), diverting excess visual and backfilling with deeper text, RRF order
   preserved.
7. **Truncate to `top_n`** and return.

### Dedup granularity (decided)

De-dup by **exact `source` string** — `file_path` for text hits, `"{file_path} (page N)"`
for visual hits. This collapses the observed redundancy (same `.md` repeated; the same
`(page N)` repeated) while keeping *different* pages of one PDF as distinct results,
preserving page-level visual diversity. Rejected alternative: collapsing all pages of a
PDF to one slot (max doc diversity, but a multi-page datasheet could surface only once).

## Components & interfaces

### `_dedupe_by_source(results: list[dict]) -> list[dict]` (new, pure)

Returns a new list with later duplicates of each `source` removed, first occurrence and
order preserved. Hits without a `source` key (defensive) are passed through untouched
(treated as distinct). No I/O; trivially unit-testable.

### `_apply_visual_cap(ordered: list[dict], limit: int, visual_max_ratio: float) -> list[dict]` (extracted)

Extracted verbatim from the existing cap+backfill block inside `_rrf_merge_collections`
(currently lines ~1692–1713): given an already-ordered hit list, admit up to `limit`
hits, capping `type == "visual"` hits at `round(visual_max_ratio * limit)`, diverting
overflow visual and backfilling with deeper non-visual hits, order preserved. Returns a
list of length ≤ `limit`.

After extraction, `_rrf_merge_collections` calls `_apply_visual_cap` internally (no
behavior change to existing callers), and `run_search` calls it again at the final stage
against `top_n`. The logic exists once, used in two places.

### `run_search` changes

- Compute `pool_limit = max(fetch_limit, _RESULT_POOL_FLOOR)` when `dedupe_results` is on.
- Merge with `visual_max_ratio=1.0` (defer cap); rerank with `top_n=pool_limit`.
- Final stage: `_dedupe_by_source` → `_apply_visual_cap(..., top_n, ...)` → `[:top_n]`.
- When `dedupe_results` is off: unchanged from today (shallow fetch, cap during merge,
  no dedup) — exact baseline for A/B.

## No-regression argument (default rerank-off configuration)

Doc-level recall@`top_n` cannot drop a doc the old path returned:

- **Visual count is unchanged.** Old path caps visual at `round(ratio * fetch_limit)`
  with `fetch_limit = top_n` → 1 (at defaults). New path caps at `round(ratio * top_n)`
  → 1. Same single highest-RRF visual is admitted in both (dedup keeps the first/best
  occurrence), so the visual slot is identical.
- **Text docs are a superset.** The merged RRF order's prefix is identical between old
  (shallow) and new (deep) — deepening only appends a tail. The old top-`top_n` chunks
  cover the first *k ≤ top_n* distinct docs of that order; the new path returns the first
  `top_n` distinct docs of the same order. The former is a prefix-subset of the latter,
  so every doc shown by the old path is still shown.

Therefore the new path's distinct-doc set ⊇ the old path's, and recall is monotonically
non-decreasing. The guarantee is also verified empirically by the eval A/B (the strict
acceptance bar). The guarantee is stated for the default rerank-off path; the rerank-on
path (opt-in, hook-disabled) changes — for the better, feeding the reranker distinct docs
— and is validated separately if/when rerank is enabled.

## Error handling

- Both helpers are pure and total; no new I/O, no new failure modes.
- The existing per-collection fetch error handling (skip-on-404, raise-on-transport) is
  unchanged. Fail-open posture preserved.

## Config & defaults

- `search.dedupe_results: true` — new key in `DEFAULTS` (`carta/config.py`). Default on
  (it is a bug fix); off restores prior behavior for A/B and rollback.
- `_RESULT_POOL_FLOOR = 30` — module constant in `pipeline.py` (not config; internal
  headroom). Promote to config only if a future need to tune it appears (YAGNI).

## Testing (TDD)

1. `_dedupe_by_source` — unit: collapses repeated sources to first occurrence; preserves
   order; keeps distinct sources (incl. different `(page N)` of one PDF); passes through
   hits lacking `source`.
2. `_apply_visual_cap` — unit: caps visual at `round(ratio*limit)`; backfills text when
   text is shallow; `ratio=1.0` is a no-op; order preserved. (Characterization tests so
   the extraction is provably behavior-preserving for `_rrf_merge_collections`.)
3. `run_search` — integration (mocked Qdrant): a dup-heavy fused list yields `top_n`
   **distinct** sources; visual stays ≤ `round(ratio*top_n)`; flag off reproduces current
   output.
4. Acceptance — ET-embed eval A/B: recovers `cts-control`, recall@5 ≥ 0.952 → ~0.968,
   **zero regressions**.

## Risk & rollback

- **Risk:** the cap-extraction subtly changes `_rrf_merge_collections` output.
  *Mitigation:* characterization tests pin its current behavior before extraction.
- **Risk:** deeper `_visual` fetch adds latency. *Mitigation:* ColPali query embed is
  per-query (one model load); fetching 30 vs 5 points from Qdrant is negligible. The hook
  disables visual/rerank anyway.
- **Rollback:** `search.dedupe_results: false` restores prior behavior with no redeploy.

## Out of scope / follow-ups (tracked, not in this spec)

- **Eval-correctness pass** — FSM underscore/hyphen expectation; patent-only golds where
  relevant markdown exists; re-verify the other 59. Makes the metric trustworthy.
- **#38** — OCR-drain scanned patents so `US-11965795` enters the index.
- **Duplicate `_visual` points** — the same page appearing twice (`(page 20) ×2`) hints
  at stale visual points from re-drains; a `_visual` data-hygiene/repair item.
