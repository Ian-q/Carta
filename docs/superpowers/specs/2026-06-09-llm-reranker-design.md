---
id: 2026-06-09-llm-reranker-design
title: "Design — LLM reranker backend for `search.rerank`"
status: shipped
related:
  - 2026-06-09-llm-reranker
date: 2026-06-09
---

# Design — LLM reranker backend for `search.rerank`

**Date:** 2026-06-09 · **Status:** approved (brainstorm) · **Target:** carta-cc (next minor, 0.8.0)

## Problem

On the ET-embed corpus, hybrid retrieval lands every relevant doc in the candidate
pool — `recall@50 = 1.000` — but several land too low for top-5:

| Query | rank |
|---|---:|
| Safety MCU CAN message IDs and heartbeat frames | 33 |
| telemetry timing architecture and rates | 43 |
| VCU connector pinout and connector map | 18 |
| virtual load cell estimation | 8 |
| VCU power architecture | 6 |

So `recall@5` (0.700) is bottlenecked by **ranking**, not retrieval. The only reranker in
fastembed's catalog, `BAAI/bge-reranker-base`, was tested at `candidate_pool` 30 **and** 50 —
both net 0.700 (it just reshuffles which 6 miss). It is too weak to float these subtly-relevant
docs. A stronger reranker is the highest-ROI, graph-independent lever.

(Graph-aware retrieval — 1-hop `related:` expansion — is the complementary lever and the
Carta-native one, but it depends on normalizing + densifying the `related:` graph via the audit/
linking work. It is **out of scope here** — a separate phase-2 spec.)

## Goals / Non-goals

**Goals**
- Add an **LLM reranker backend** to the existing `search.rerank` stage, selectable by config.
- Single listwise Ollama call per search (not N pointwise calls).
- Strictly additive + fail-open: default behavior and the existing cross-encoder path unchanged;
  any LLM error/timeout falls back to the fused order (never worse than today).
- Measurable: re-run `et-embed.yaml`; success = pulling rank-8–43 docs into top-5 above 0.700.

**Non-goals**
- Graph-aware retrieval / `related:` traversal (phase 2).
- The reranker→link-discovery feedback loop (phase 3).
- Any new external/paid API. Local Ollama only.
- Changing the hybrid fetch or fusion.

## Design

### Integration point
`run_search` (`carta/embed/pipeline.py`) already fetches `candidate_pool` candidates when
`search.rerank.enabled` and calls `rerank_hits(query, pool, model_name, top_n)` from
`carta/search/rerank.py`. We dispatch on a new `backend` key:

- `backend: cross-encoder` (**default**) → existing `rerank_hits` (fastembed). Unchanged.
- `backend: llm` → new `llm_rerank_hits(...)`.

### New unit — `carta/search/llm_rerank.py`
```
llm_rerank_hits(query, hits, *, model, ollama_url, top_n,
                timeout_s=20, max_excerpt_chars=500) -> list[dict]
```
One purpose: reorder `hits` by LLM-judged relevance to `query`, return top_n. Pure-ish: its only
dependency is one Ollama `/api/chat` HTTP call (same plumbing as `carta/hook/hook.py`).

**Mechanism (listwise, one call):**
1. Build a numbered list of candidates: `[i] {source}\n{excerpt[:max_excerpt_chars]}` for each hit.
2. Prompt: *"Rank the passages most relevant to the query. Return ONLY a JSON array of the
   passage numbers, most relevant first, length ≤ top_n."* (`format: json`, low temperature).
3. Parse the returned index array; reorder `hits` accordingly; append any unranked hits in their
   original fused order; truncate to `top_n`.
4. Stamp `rerank_score` as a descending synthetic rank (so output shape matches the cross-encoder
   path, which `run_search` already strips).

**Fail-open** (return fused `hits[:top_n]` unchanged) on: Ollama unreachable/timeout, non-JSON or
malformed reply, empty/blank query, or zero hits. Never raises into `run_search`.

### Config (`search.rerank`)
```yaml
search:
  rerank:
    enabled: true            # existing
    backend: cross-encoder   # NEW: cross-encoder | llm  (default cross-encoder)
    model: BAAI/bge-reranker-base   # used when backend=cross-encoder
    llm_model: qwen3.5:0.8b         # NEW: used when backend=llm (local Ollama) — small-first
    llm_timeout_s: 20               # NEW
    candidate_pool: 50              # bump default? see Open Questions
```
`ollama_url` is read from `embed.ollama_url`. Defaults keep the cross-encoder path; `backend: llm`
is opt-in. DEFAULTS in `carta/config.py` gain `backend`, `llm_model`, `llm_timeout_s`.

### Data flow
`run_search` → hybrid fetch (`candidate_pool` hits, fused) → if `rerank.enabled`: dispatch on
`backend` → (`llm` → one Ollama call → reordered top_n | `cross-encoder` → fastembed) → strip
transient keys → return top_n. Visual/RRF fusion path unchanged.

### Error handling
- Ollama down / timeout / 5xx → log one stderr line, return fused top_n.
- Reply not parseable as a JSON index array → fail-open.
- Indices out of range / duplicated → filter to valid/unique, fill remainder from fused order.
- `backend: llm` but `embed.ollama_url` unset → fail-open + warn once.

### Testing (TDD)
Pure-function tests with a mocked Ollama call (`requests.post`), no live model:
- Reorders to the LLM's returned index order; truncates to `top_n`.
- Unranked candidates appended in original order (no dropped hits).
- Fail-open: timeout, non-JSON, out-of-range indices, empty hits → fused order, never raises.
- `run_search` dispatch: `backend: llm` calls `llm_rerank_hits`; `cross-encoder`/unset calls the
  existing path (monkeypatch both; assert the right one fires).
Live eval (manual, not CI): `carta eval .carta/eval/et-embed.yaml -k 5` with `backend: llm` vs the
0.700 cross-encoder/none baseline.

## Open questions (resolve in plan)
1. **Default `candidate_pool`** — keep 30 or bump to 50? Bigger pool = more context for the LLM but
   a longer prompt. Lean 40.
2. **`llm_model` default** — **decided: start with `qwen3.5:0.8b`** (fast, already the recall
   judge). Bench it against `qwen3.5:9b` on the eval; keep the small model unless 9b is
   *significantly* better on recall. Open to trying another local model if one looks well-suited.
3. **Excerpt budget** — `max_excerpt_chars` 500 × ~40 candidates ≈ 20k chars; fits small-model
   context but verify against the chosen model.

## Phasing
- **Phase 1 (this spec):** LLM reranker backend.
- **Phase 2:** normalize the `related:` graph (id-vs-path) + graph-aware 1-hop expansion in
  `run_search`, fed by the audit/linking work.
- **Phase 3:** mine reranker signal to *suggest* new `related:` links back into the audit (the flywheel).
