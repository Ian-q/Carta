# Design — Visual-cap fusion (bound the `_visual` lane's share of the fused pool)

**Date:** 2026-06-12 · **Status:** approved (brainstorm) · **Target:** carta-cc (next minor, 0.12.0) · **Phase:** 1 of 2 — the v0.12.0 retrieval-quality cycle (follows v0.11.0 data integrity [2026-06-12-data-integrity-design.md](2026-06-12-data-integrity-design.md); precedes #37 reranker demotion). Closes [#36](https://github.com/Ian-q/Carta/issues/36).

## Problem

`_rrf_merge_collections` (`carta/embed/pipeline.py:1509`) fuses the per-collection hit
lists with Reciprocal Rank Fusion: every hit's fused score depends **only on its rank
within its own collection** (`rrf = 1/(k + rank + 1)`). A text rank-0 hit and a visual
rank-0 hit therefore score identically and tie; text and visual interleave ~1:1 all the
way down, and the merged list is truncated to `fetch_limit`.

The consequence: **whenever a `_visual` collection holds content, ~half of every query's
fused pool is visual — including pure-text questions.** Effective text depth is silently
halved. Text docs at text-fused rank `pool/2 .. pool` can never reach the second stage
(or, with rerank off, the returned `top_n`).

Measured on the 62-query ET-embed eval (2026-06-12, post v0.11.0 repair, hybrid-alone):
three misses are wholly or partly dilution —

| Query | expected doc | note |
|---|---|---|
| SAFETY-MCU-MESSAGES | `docs/CAN/SAFETY-MCU-MESSAGES.md` | text-fused ~#34 → absent from the merged pool |
| TIMING_ARCHITECTURE | telemetry timing doc | text rank survives single-collection, lost after merge |
| connector-map | `docs/hardware/vcu/connector-map.md` | text #18 → merged #35 |

Two structural facts shape the fix:

- **The merge feeds both retrieval paths.** `run_search` calls `_rrf_merge_collections`
  once (`pipeline.py:1738`); its output is *both* the hybrid-alone `top_n` return *and*
  the `candidate_pool` slice handed to the reranker (`:1750`). A fix here lifts text depth
  in **both** the 0.839 hybrid number and the 0.903 reranked number, in one place.
- **Only `_visual` is scale-incompatible.** `per_collection` can hold several *text*
  collections (`doc`, `session`, `quirk`, `note`) plus one `_visual`. The text collections
  share a comparable cosine/RRF scale; RRF among them is fine. The intruder is specifically
  the visual lane (ColPali MaxSim, ~10–40). The fix must treat **"all text lanes" vs "the
  one visual lane"**, not collection-by-collection. Hits already carry the discriminator:
  text hits are stamped `"type": "text"` (`:1717`), visual `"type": "visual"` (`:1668`).

## Goals / Non-goals

**Goals**
- **Cap the visual lane's share of the fused pool** at a configurable fraction, preserving
  RRF ordering among everything admitted. Excess visual hits are dropped (not interleaved),
  and the slots they vacated are backfilled with deeper text.
- **No-op for pure-text corpora.** When no hit has `type == "visual"`, output is byte-identical
  to today. Pure-text projects (the common case) see zero behaviour change.
- **One config knob:** `search.fusion.visual_max_ratio`, defaulted in `config.py` DEFAULTS.
  Because the cap is a fraction of pool size, it auto-scales across regimes (hybrid-alone
  pool=5, reranked pool=30/40, graph pool=50).
- **Fix both paths at once** by living in `_rrf_merge_collections` — helps hybrid-alone
  (the ET-embed default, rerank off) and reranked alike.
- **Measurable, eval-swept default:** choose the shipped `visual_max_ratio` by sweeping it
  on *both* eval sets (below). Success = the three dilution misses reach top-5 on the
  62-query eval (hybrid-alone and reranked) **with no regression on the 14-query visual eval**.

**Non-goals**
- #37 (reranker demoting correct first-stage hits) — the next phase of this cycle.
- Score-gated / relevance-thresholded visual inclusion — reintroduces the cross-scale
  comparison RRF exists to avoid; fragile thresholds across corpora. Rejected during brainstorm.
- Widening `fetch_limit` / the rerank pool to fit both lanes — raises rerank latency
  (already ~10–15 s/query) and papers over, rather than fixes, the 50/50 split. Rejected.
- A per-query "is this a visual question?" classifier — out of scope; the cap is a static dial.
- Draining the 55 `--visual`-queued ET-embed patents — separate work (#38); intentionally
  **after** this fix, since draining more visual before capping would amplify dilution.

## Design

### Mechanism — capped RRF merge

A single change inside `_rrf_merge_collections`. After the existing RRF sort produces the
fused, best-first list, walk it once with a visual budget:

```
visual_cap = round(visual_max_ratio * top_n)      # top_n == the pool size being composed
result, overflow, visual_admitted = [], [], 0
for hit in rrf_sorted:
    if len(result) == top_n:
        break
    if hit is visual (hit.get("type") == "visual"):
        if visual_admitted < visual_cap:
            result.append(hit); visual_admitted += 1
        else:
            overflow.append(hit)            # divert, preserve order
    else:
        result.append(hit)                  # text admitted freely
# text ran out before filling the pool → backfill from diverted visual, still RRF order
if len(result) < top_n:
    result.extend(overflow[: top_n - len(result)])
return result
```

Properties:
- **RRF order is preserved** among admitted hits — the cap only removes excess visual and
  pulls up whatever text/visual sat behind it. No re-scoring, no scale comparison.
- **No-op when no visual lane:** with zero visual hits the visual branch never fires and the
  walk reproduces the current `scored[:top_n]`. (Also a no-op when `visual_cap >= top_n`,
  i.e. `visual_max_ratio >= 1.0` — the explicit "disable the cap" setting.)
- **Backfill guarantees length:** when text is too shallow to fill the pool, diverted visual
  is restored, so the merge never returns fewer hits than it does today.
- **Latency-neutral:** pool size is unchanged; the reranker sees the same number of candidates.

The signature stays `_rrf_merge_collections(per_collection, top_n, k=60)` with one new
keyword arg `visual_max_ratio: float = 1.0` (default = no cap, so existing callers/tests are
unaffected until `run_search` passes the configured value). `run_search` reads
`cfg["search"]["fusion"]["visual_max_ratio"]` and passes it at the `:1738` call site.

### Config surface

New `fusion` block in `config.py` DEFAULTS under `search`, beside `hybrid`/`rerank`/`graph`:

```yaml
search:
  fusion:
    # Ceiling on the visual (_visual/ColPali) collection's share of the fused candidate
    # pool, as a fraction of pool size. RRF interleaves text and visual ~1:1 by rank, which
    # halves text depth on every query once a _visual collection exists; this bounds visual
    # so text questions keep their depth. 1.0 disables the cap (legacy behaviour). No effect
    # on pure-text corpora. Default is the eval-swept optimum (see RESULTS.md 2026-06-12).
    visual_max_ratio: <swept-best>   # provisional 0.34 pending the sweep
```

The shipped default is the value chosen by the sweep below — set during implementation, not
guessed here. `_deep_merge` of DEFAULTS + user YAML (existing mechanism, `config.py:147`
already deep-merges the `search` subtree) means existing project configs inherit the new
key without edits.

### Data flow (unchanged except the merge)

```
run_search
  → per-collection query (text lanes + _visual), each to fetch_limit          (unchanged)
  → _rrf_merge_collections(per_collection, fetch_limit, visual_max_ratio=…)    (← capped here)
  → [graph expansion if enabled]                                              (unchanged)
  → [rerank top candidate_pool if enabled]                                    (unchanged)
  → return top_n                                                              (unchanged)
```

## Testing (TDD, unit-level on the merge)

New cases in the merge's test module (`carta/embed/tests/test_visual_search_merge.py`):

1. **No-op without visual:** all-text `per_collection` → output identical to the un-capped
   `scored[:top_n]` (regression guard for pure-text corpora).
2. **Cap binds:** text-heavy fused list, `visual_max_ratio` giving `cap=1` on a pool of 5 →
   exactly 1 visual in output, ≥4 text, RRF order preserved among admitted.
3. **Text exhausted → overflow backfill:** few text hits, many visual, small cap →
   result length == `top_n`, filled from diverted visual in RRF order.
4. **Order preservation:** admitted hits appear in the same relative order RRF gave them.
5. **`visual_max_ratio = 1.0` disables the cap:** output == current behaviour (cap ≥ pool).
6. **Rounding edge:** `cap == 0` (ratio rounds to 0) drops all visual when text fills the
   pool; documents/asserts the chosen rounding (`round`).

Full suite (851 + new) green on 3.10–3.12 before PR, per house practice.

## Validation — the ratio sweep (deliverable: the shipped default)

The cap is a Pareto dial, not a constant: too loose and text stays diluted; too tight and
the visual eval's class-2 queries (the `(page N)` suffix appears **only** on `_visual`
results — text chunks can never satisfy them, and the visual eval runs **rerank-off**) lose
their visual hits. So the default is chosen empirically against *both* sets.

Run from the ET-embed root with the unreleased checkout
(`PYTHONPATH=<carta-checkout> ~/.local/pipx/venvs/carta-cc/bin/python -m carta eval …`,
`OMP_NUM_THREADS=1` when reranking), sweeping `visual_max_ratio ∈ {1.0, 0.5, 0.34, 0.2}`:

| Eval set | config | metric watched | direction |
|---|---|---|---|
| `et-embed.yaml` (62q) | hybrid-alone | recall@5 / MRR | maximize (recover the 3 misses) |
| `et-embed.yaml` (62q) | + qwen3.5:9b rerank, pool 40 | recall@5 / MRR | maximize / hold ≥ 0.903 |
| `et-embed-datasheets.yaml` (14q) | auto (rerank off) | recall@5 (esp. class-2) | **must not regress** |

Pick the lowest-dilution ratio that maximizes 62-query text recall while holding the visual
eval flat; ship it as the `config.py` default. Record the full table in `RESULTS.md`
(dated section) and update `MEMORY.md` project memory (`et-embed-eval-workflow`) with the
new baseline and the chosen ratio.

## Rollout / risk

- **Behaviour change for visual-enabled projects only.** Approved to ship the swept-best
  ratio as the new default; pure-text projects are unaffected, and any project can restore
  legacy fusion with `search.fusion.visual_max_ratio: 1.0`.
- **Fail-safe shape:** the merge cannot raise on the new path (pure list walk); worst case a
  mis-set ratio changes pool composition, never breaks search. No fail-open wrapper needed.
- **Interaction with graph expansion (#graph):** graph runs *after* the capped merge on the
  already-composed pool — no special handling; the cap simply shapes what graph sees. Off by
  default on ET-embed, so the sweep measures the cap in isolation.
