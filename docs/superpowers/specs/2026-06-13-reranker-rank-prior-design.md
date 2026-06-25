# Design — Reranker rank-prior blend (stop the LLM reranker demoting good first-stage hits)

**Date:** 2026-06-13 · **Status:** approved (brainstorm) · **Target:** carta-cc (next minor, 0.12.0) · **Phase:** 2 of 2 — the v0.12.0 retrieval-quality cycle (follows visual-cap fusion [2026-06-12-visual-pool-dilution-design.md](2026-06-12-visual-pool-dilution-design.md), #36). Closes [#37](https://github.com/Ian-q/Carta/issues/37).

## Problem

The listwise LLM reranker (`carta/search/llm_rerank.py`) sends the fused candidate pool to a
small Ollama model (qwen3.5:9b on ET-embed), which returns a JSON array of the passage numbers
it judges *"clearly relevant,"* most-relevant-first. The current ordering is:

```
merged = [hits[i] for i in order]            # model-listed, model order
       + [h for j,h in enumerate(hits) if j not in order]   # everything else, fused order
```

So **passages the model omits get dumped after every passage it listed, and first-stage
(RRF-fused) rank carries zero weight.** When the small model omits or low-ranks a correct
passage, that passage sinks out of top-5 even though first-stage retrieval ranked it highly.

Diagnostic on the 62-query ET-embed eval (2026-06-13, post-#36, reranked pool 40):

| reranked miss | first-stage fused rank | cause |
|---|---|---|
| RJ45-CTS pinout | **#7** | in-pool; reranker **demotes** it → this design fixes it |
| SAFETY-MCU messages | **#44** | just *outside* the 40-pool — reranker never sees it → pool bump |
| telemetry timing | not in top 50 | first-stage recall ceiling → **out of scope** |
| kingpin prior-art | not in top 50 | first-stage recall ceiling → **out of scope** |

First-stage recall@50 is 0.968 (2/62 unreachable). This design targets the **demotion** of
in-pool hits (RJ45-CTS) plus a pool-depth bump (SAFETY-MCU). The two recall-ceiling misses are
a separate embeddings/chunking problem, explicitly out of scope.

## Goals / Non-goals

**Goals**
- **Blend the LLM ranking with a first-stage rank prior** so a strong first-stage hit the model
  omits/demotes cannot be sunk below low-ranked passages the model happened to promote.
- **Omission-neutral handling:** a passage the model does not list keeps its first-stage standing
  instead of being dumped to the bottom.
- **One tunable knob, eval-swept:** `search.rerank.first_stage_weight`, shipped at the swept
  optimum; `1.0` degenerates to pure first-stage (no rerank).
- **Deeper rerank pool:** `search.rerank.candidate_pool` 30 → 50, to bring boundary docs
  (SAFETY-MCU at #44) into the pool — *validated*, since a longer listwise prompt can degrade a
  small model's ranking.
- **On by default** when reranking is enabled (reranking itself stays opt-in). Preserve the
  reranker's net win (the #36-measured reranked recall 0.935) while eliminating the demotion.
- **Fail-open unchanged:** any model error/timeout/empty-parse still returns the fused order.

**Non-goals**
- Prompt rewrites (force-rank-all / anti-demotion instructions) — held as a follow-up if the
  sweep shows the model omits too aggressively for the blend to compensate.
- First-stage recall work for telemetry / kingpin (not retrievable in top-50) — a separate issue.
- The cross-encoder backend path (`rerank_hits`) — unchanged; this design touches only the LLM
  backend.

## Design

### Blend formula — a pure helper

The fused `hits` arrive in first-stage order, so a passage's **input index is its first-stage
rank**. Score every passage by a weighted blend of two reciprocal ranks (k=60, matching the
existing RRF constant), then sort:

```python
def _blend_order(n: int, order: list[int], first_stage_weight: float, k: int = 60) -> list[int]:
    """Return final 0..n-1 ordering blending the LLM ranking (`order`) with first-stage rank.

    `order` is the model's listed indices, best-first (a subset of range(n), de-duplicated,
    in range — exactly what _parse_order returns). Passages absent from `order` are treated
    omission-neutral: their LLM reciprocal rank equals their first-stage reciprocal rank, so
    the model neither promotes nor demotes them.
    """
    listed_pos = {idx: pos for pos, idx in enumerate(order)}
    scored = []
    for i in range(n):
        fs_rr = 1.0 / (k + i)
        llm_rr = 1.0 / (k + listed_pos[i]) if i in listed_pos else fs_rr
        score = (1.0 - first_stage_weight) * llm_rr + first_stage_weight * fs_rr
        scored.append((score, i))
    scored.sort(key=lambda t: (-t[0], t[1]))   # score desc, then first-stage rank asc (deterministic)
    return [i for _, i in scored]
```

Properties (all unit-tested):
- `first_stage_weight == 1.0` → every score is `fs_rr` → identity order → **pure first-stage,
  no rerank**.
- `first_stage_weight == 0.0` with a full permutation `order` → the model's order exactly.
- An **omitted** passage's score is `fs_rr` regardless of weight, so it holds its first-stage
  position; raising `first_stage_weight` drags down *listed* passages with weak first-stage rank,
  letting an omitted strong-first-stage hit (RJ45-CTS at #7) climb back above mis-promoted junk.

### Wiring — `llm_rerank_hits`

The success path replaces the `merged = ranked + remainder` block:

```python
order = _parse_order(content, len(hits))
if not order:
    return hits[:top_n]
final = _blend_order(len(hits), order, first_stage_weight)
ranked = [hits[i] for i in final]
for rank, h in enumerate(ranked):
    h["rerank_score"] = float(len(ranked) - rank)   # presence signals rerank_applied; stripped downstream
return ranked[:top_n]
```

`llm_rerank_hits` gains a `first_stage_weight: float = 0.0` parameter. `rerank_dispatch`
(`rerank.py`) passes `rr_cfg.get("first_stage_weight", 0.0)`. Fail-open branches (`not order`,
exception, empty query) are untouched.

### Config

In `config.py` `DEFAULTS["search"]["rerank"]`:
- add `"first_stage_weight": 0.3` (provisional; finalized by the sweep, see Validation),
- change `"candidate_pool": 30` → `"candidate_pool": 50`.

`_deep_merge` carries both into existing project configs without edits. `first_stage_weight`
only affects projects with `rerank.backend: "llm"` and `rerank.enabled: true` (opt-in).

## Testing (TDD)

Unit tests on the pure helper (`carta/search/tests/test_llm_rerank.py`, extending existing):
1. `first_stage_weight=1.0` → identity (no rerank), for any `order`.
2. `first_stage_weight=0.0`, `order` a full permutation → returns `order`.
3. Omission-neutral: with some indices absent from `order`, omitted passages keep first-stage
   relative order and are not all forced behind every listed passage.
4. Demotion rescue: an omitted passage at first-stage rank 1 outranks a listed passage at
   first-stage rank 20 once `first_stage_weight` ≥ a threshold the test pins with explicit values.
5. Determinism: equal scores break ties by first-stage rank.

Plus a `llm_rerank_hits` test (monkeypatching the Ollama call) confirming the blended order is
applied and `rerank_score` stamped; and that fail-open paths still return the fused order.
Existing `llm_rerank` tests that assert the old `ranked + remainder` ordering are updated to the
blend behavior. Full suite green on 3.10–3.12.

## Validation — sweep (deliverable: shipped defaults)

Reranked eval is expensive (~10–15 s/query) but **temperature=0**, so single runs are
reproducible. Run `caffeinate -ims` background (a sleeping laptop fail-opens Ollama and corrupts
the run — verify 0 fail-opens before trusting numbers), from the ET-embed root with the worktree
checkout (`PYTHONPATH=<worktree> ~/.local/pipx/venvs/carta-cc/bin/python -m carta eval … -k 5`,
`OMP_NUM_THREADS=1`, rerank block enabled).

1. **Weight sweep at `candidate_pool=40`:** `first_stage_weight ∈ {0.0, 0.2, 0.3, 0.4, 0.5}`.
   Success = RJ45-CTS recovered (in top-5) while reranked recall@5 ≥ 0.935 (the #36 baseline) and
   no previously-hit query regresses. Pick the lowest weight that achieves it (least disturbance
   to the reranker's promotions).
2. **Pool check at the winning weight:** re-run at `candidate_pool=50`; confirm SAFETY-MCU (#44)
   enters and is surfaced, recall holds or improves, and per-query latency stays acceptable
   (record s/query at 40 vs 50).
3. Ship the chosen `first_stage_weight` + `candidate_pool` as `config.py` defaults (update the
   config test in lockstep). Record the sweep table in the ET-embed `RESULTS.md` and update the
   `et-embed-eval-workflow` project memory.

Baseline reference (post-#36, this corpus): reranked recall@5 **0.935** / 4 misses
(RJ45-CTS, SAFETY-MCU, telemetry, kingpin). Target after this change: RJ45-CTS + SAFETY-MCU
recovered → **~0.968**, telemetry/kingpin still missing (out of scope).

## Rollout / risk

- **Behaviour change for LLM-rerank users only.** Reranking is opt-in and off on ET-embed by
  default; `first_stage_weight: 1.0` disables the blend (pure first-stage), and the cross-encoder
  backend is untouched.
- **Pool bump cost:** 50 vs 40 candidates lengthens the single listwise prompt; the sweep
  validates that ranking quality and latency remain acceptable, otherwise the pool default stays
  lower and SAFETY-MCU is left to first-stage work.
- **No new failure modes:** `_blend_order` is a pure list operation; all existing fail-open paths
  (timeout, empty parse) are preserved and still return the fused order.

---

## OUTCOME — ABANDONED (eval-disproven, 2026-06-14)

This branch is kept **stale and unmerged** as a record of a pathway that did not work. Do not
merge it. The implementation is complete and tested (4 commits, 866 tests green), but the eval
disproved the premise.

**What the eval showed** (ET-embed 62q, reranked qwen3.5:9b, pool 40, 0 fail-opens):
- Rank-prior blend swept `first_stage_weight ∈ {0,0.2,0.3,0.4,0.5}`: RJ45-CTS **and** SAFETY-MCU
  miss at **every** weight; reranked recall@5 never beats 0.903 (≤ baseline) and trends down.
- Pivot — tighter/absolute visual cap: tightening the #36 cap lifts RJ45's *fused* rank
  (7→6→4 as cap goes 8→2→0), but only cap 0 (no visual — globally unacceptable) reaches fused
  #4, and a decisive reranked run at **cap 1** still shows RJ45 **MISS** in the reranked top-5.
- The new `w=0` blend also regressed 2 previously-hit queries (FSM, through-the-road) vs the old
  reranker — net-negative.

**Why (corrected root cause):** the residual reranked misses are **not reranker demotions** —
they are **first-stage retrieval quality**. RJ45-CTS's gold doc sits at text-fused ~#4–5 and the
reranker doesn't value it; SAFETY-MCU is at fused #44; telemetry / kingpin aren't in the top 50
at all (first-stage recall@50 = 0.968). No reranking-pool change recovers them. The original
demotion #37 was filed for (US10245972) was already resolved by the v0.11.0 repair + #36.

**Real lever (future work):** first-stage recall — embeddings / chunking / query expansion
(issue #19), a separate and larger investigation.
