# Retrieval Path Repair and Tracing

**Date:** 2026-08-09
**Status:** Approved (brainstormed with Ian, session "Flath OCR research")
**Motivating failure:** two reproduced live bugs, below.

## Case study — why this exists

While reading Carta against Isaac Flath's retrieval course and Joe Barrow's OCR talks, Flath's insistence on per-stage observability prompted a direct measurement of the query path rather than a code read. Two bugs surfaced, both silent, both disabling a primary path.

**Bug A — MCP `carta_search` returns `[]` on every hybrid collection.**
`carta/mcp/server.py:261` calls `query_points()` with no `using=`. Every hybrid-embedded collection has *named* vectors (`dense`) plus a sparse vector (`bm25`), and Qdrant rejects an unnamed query against them. The exception is wrapped as `RuntimeError` and swallowed by `except RuntimeError: pass` at `server.py:133-135` — the handler intended for "this collection does not exist." The tool therefore reports *no results* rather than an error.

Reproduced against the live homelab Qdrant:

```
TARGET ET-embed_doc: points=8178 named_vectors=['dense'] sparse=['bm25']

--- query_points WITHOUT using=  (what server.py:261 does) ---
FAILED: UnexpectedResponse 400 (Bad Request)
  {"status":{"error":"Wrong input: Not existing vector name error: "}}

--- query_points WITH using='dense' ---
OK, points returned: 3
```

This is the path Claude uses to query Carta. It also serves the `_notes` collections, so notes captured by `carta remember` are unreachable through MCP. Legacy collections with unnamed vectors (`Elementrailer_doc`) still work, which is likely why it went unnoticed. Every MCP test patches the Qdrant client, so nothing ever exercised a real named-vector collection.

**Bug B — the proactive-recall gate compares RRF scores against cosine thresholds.**
`hook.py:108-113` gates on absolute `hits[0]["score"]` against `low_threshold` 0.60 / `high_threshold` 0.85 (`config.py:144-145`). Those constants are calibrated for cosine similarity. But `search.hybrid.enabled` defaults `true` (`config.py:47`), so the returned scores are Qdrant's server-side Reciprocal Rank Fusion output — `Σ 1/(k+rank)` with **k=2** — a bounded, discrete rank statistic, not a similarity.

Measured on the ET-embed corpus:

```
gate thresholds: low=0.6  high=0.85

0.5769231   trailer axle load rating
0.64285713  brake controller wiring
0.5833333   suspension mount
1.0         torsion axle specifications
0.5909091   wire gauge for lights
```

Every value decomposes exactly: `0.5769 = 1/2 + 1/13` (rank 0 in one lane, rank 11 in the other), `0.6428 = 1/2 + 1/7`.

**The score is not the number that ranked the hit.** Carta runs RRF at two levels:

| Layer | Where | `k` | Fuses |
|---|---|---|---|
| Intra-collection | Qdrant, server-side (`pipeline.py:1827`) | 2 | dense + sparse lanes |
| Cross-collection | Python, already client-side (`pipeline.py:2490`) | 60 | doc / notes / visual |

`run_search` stores each hit's *per-collection* Qdrant score at `pipeline.py:2457`, then `_rrf_merge_collections` reorders the hits but never assigns its fused score back onto them. So `hits[0]["score"]` is the intra-collection k=2 fusion, while the ordering was decided by the cross-collection k=60 fusion. The gate reads a magnitude that is both on the wrong scale *and* disconnected from the ranking it is trying to gate.

Three further consequences follow arithmetically:

1. A hit ranked **first** in one lane but deep in the other maxes around 0.50–0.58 — below `low_threshold`. Dropped silently, without reaching the judge. Three of the five queries above.
2. The maximum possible score is 1.0 (rank 0 in both lanes). The *second*-best achievable combination is `1/2 + 1/3 = 0.833`, below `high_threshold`. The fast-path inject can therefore fire **only on an exact 1.0**.
3. The judge's gray zone is a narrow, oddly-shaped sliver rather than the intended band.

The hook fails open and exits 0 on every path, so this is invisible in normal use.

## Goals

1. `carta_search` returns results on named-vector collections, and reports errors as errors.
2. Per-lane ranks are available to the search path, so gating and diagnosis can use retrieval *structure* rather than an uninterpretable fused magnitude.
3. Every hook invocation and every traced search records where a result sat at each stage.
4. The recall gate is scale-independent and calibratable from recorded data.
5. Legacy unnamed-vector collections keep working.

## Non-goals (deliberate)

- **Chunker and page-furniture rewrite.** Real and verified, but it invalidates vectors and re-queues the visual drain (`pipeline.py:1497`). Separate spec.
- **Surya 2 adoption.** Depends on the chunker work landing first for a shared re-embed.
- **Eval-set rebuild and the visual inspector.** Depend on this spec; the inspector additionally depends on an OCR backend that emits geometry, which none currently does.
- **Changing RRF `k` from 2 to 60.** Explicitly deferred — see Component 2b. Changing fusion semantics without an eval set is how you regress retrieval invisibly.
- **Session-end relevance feedback** (#118) and **agent-driven metadata enrichment** (#119). Filed separately; #118 is the intended source of real calibration data once this lands.

## Design

### Component 1 — MCP query fix

`carta/mcp/server.py` must pass `using=DENSE_VECTOR_NAME` when the target collection has named vectors, and omit it when it does not. Vector naming is detected per collection rather than assumed, so `Elementrailer_doc` and friends continue to work.

The exception handling is narrowed in the same change. A failed query and a missing collection are currently the same code path; they must not be. A missing collection stays skippable; a query failure propagates and is reported to the caller, consistent with the `#79` precedent where a backend outage was being reported as "nothing embedded."

### Component 2 — intra-collection fusion moves client-side, in two steps

Scope note: **only the intra-collection layer moves.** Cross-collection fusion (`_rrf_merge_collections`) is already client-side Python at `k=60` and is not touched by this spec, beyond Component 2c below.

The dense+sparse fusion inside a collection happens server-side (`qmodels.FusionQuery(fusion=qmodels.Fusion.RRF)`, `pipeline.py:1827`), so Qdrant returns only the fused score. Per-lane ranks never come back, and they are the signal both the gate and the trace need.

Moving that fusion into Python replaces a working, load-bearing path, and there is currently **no eval set** to prove retrieval did not regress. So this splits.

**2a — behaviour-preserving refactor.** Query the `dense` and `bm25` lanes separately, fuse in Python with `k=2` to match Qdrant exactly. Verifiable without an eval set:

```python
for q in probe_queries:
    assert [h.id for h in server_fused(q)] == [h.id for h in client_fused(q, k=2)]
```

If the orderings diverge, the refactor is wrong and it is caught immediately rather than weeks later.

**2b — `k` becomes configuration, default unchanged at 2.** Moving toward Flath's standard `k=60` is a separate, deliberate, measured decision belonging to the eval work. `k` controls how sharply top ranks dominate, so changing it genuinely shifts ordering; bundling it with the refactor would alter implementation and semantics simultaneously with no instrument to detect damage.

**2c — the hit carries the score that ranked it.** `_rrf_merge_collections` currently reorders hits without recording its own fused score, leaving `hit["score"]` holding the intra-collection value (`pipeline.py:2457`). It must write the cross-collection fused score and the contributing ranks onto each hit, so that any consumer reading `hit["score"]` sees the number that actually determined the ordering. This is what makes the gate and the trace read the same reality.

Cost: two Qdrant round trips per search instead of one. On the hook's submit-blocking path this is real, though it sits inside the existing 3s search budget (`config.py`, `search_timeout_s`), and the lanes may be issued concurrently. The trace measures the actual cost.

### Component 3 — the retrieval trace

One tracing helper in the search path, two consumers. It follows the existing `_build_perf_context` / `_write_perf_log_entry` pattern (`pipeline.py:131,167`) rather than introducing a second logging mechanism — those helpers exist but are only called from `run_embed`; nothing on the search or hook path logs anything today.

**Consumer 1 — hook log.** On by default. Appends one JSONL record per invocation to `.carta/traces/hook-YYYY-MM.jsonl`, rotating monthly. `.carta/` is already gitignored.

```json
{"ts":"2026-08-09T03:41:12Z",
 "query":"torsion axle spec",
 "collections":["ET-embed_doc"],
 "lanes":{"dense":0,"sparse":4},
 "score":0.7,"score_kind":"rrf","rrf_k":2,
 "zone":"judge","judge":true,
 "latency_ms":412}
```

Records the **derived** query — the output of `_extract_query` (`hook.py:129-160`) — not the raw prompt. Enough to calibrate the gate; never persists full prompt history to disk.

**Consumer 2 — `carta search --trace <substring>`.** Prints per-stage ranks for a named document to stdout, for per-failure error analysis:

```
$ carta search "CAN termination" --trace TOPOLOGY

derived query : CAN termination
collections   : ET-embed_doc, ET-embed_visual

TOPOLOGY.md
  bm25 rank    : 3
  dense rank   : 7
  post-RRF     : 2   (score 0.700, rrf k=2)
  post-dedupe  : 2
  visual cap   : kept
  FINAL        : 2  ✓ shown
```

The stages mirror the real pipeline: per-lane retrieval → `_rrf_merge_collections` → `_dedupe_by_source` → `_apply_visual_cap` → final `top_n`.

**Trace failures are swallowed.** Instrumentation must never break search or block a prompt. A trace write that raises is caught and ignored.

### Component 4 — the gate

Replace absolute-score gating with rank and lane agreement:

- Top-`N` in **both** lanes → inject.
- Top-`N` in **one** lane → judge.
- Neither → silent.

`N` is configuration, seeded at 3. It is a placeholder, not a result: the intent is to calibrate it against recorded traces plus the session-end usage labels from #118. The advantage over the current constants is interpretability — `N=3` means "appeared in the top three," which is inspectable, whereas `0.85` meant nothing about the score scale actually in use.

Fail-open is preserved: every error path still exits 0 and lets the prompt through.

`score_kind` is recorded in the trace so cosine-scored (non-hybrid) and RRF-scored runs remain distinguishable in the calibration data.

## Testing

The reason Bug A shipped is that every MCP test patches the Qdrant client, so no test ever exercised a named-vector collection. That gap is closed here, not just the bug.

1. **Named-vector integration test.** Create an ephemeral collection with named `dense` + sparse `bm25`, upsert a point, query through the MCP path, assert non-empty. This test fails on today's code.
2. **Legacy-collection test.** Same, with an unnamed-vector collection, asserting the fix did not break the older shape.
3. **Fusion parity test** (2a). Identical result ordering between server-side and client-side fusion at `k=2` across a probe query set.
4. **Gate decision table.** Given synthetic lane ranks, assert the zone chosen — both-top-N → inject, one-top-N → judge, neither → silent — including the boundary at exactly `N`.
5. **Trace resilience.** With the trace path forced to raise, assert search still returns results and the hook still exits 0.
6. **Error propagation.** A query failure surfaces to the caller; a missing collection is still skipped.

## Risks

- **The fusion refactor regresses retrieval.** Primary risk. Mitigated by the parity assertion in 2a and by deferring the `k` change entirely.
- **Two round trips on the blocking path.** Mitigated by concurrent lane issue and the existing `search_timeout_s` budget; measured by the trace itself.
- **Legacy collections.** Mitigated by per-collection vector-name detection and test 2.
- **`N=3` is a guess.** Acknowledged, not hidden. It is strictly better than the current constants because it cannot be silently wrong about the score scale, and #118 is the path to replacing it with a measured value.

## Sequencing note

This spec is piece A+B of five. The remainder, in dependency order: **C** structure-aware chunking and page-furniture stripping; **D** Surya 2 as a geometry-emitting OCR backend (depends on C for a shared re-embed); **E** standardised task eval and the visual inspector (depends on A, B, and D — the inspector cannot draw boxes until an OCR backend emits geometry, which none does today).
