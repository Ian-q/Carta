# Design — Graph-aware retrieval (`related:` 1-hop expansion)

**Date:** 2026-06-10 · **Status:** approved (brainstorm) · **Target:** carta-cc (next minor, 0.9.0) · **Phase:** 2 of 3 (follows the LLM reranker, [2026-06-09-llm-reranker-design.md](2026-06-09-llm-reranker-design.md))

## Problem

On the ET-embed corpus, hybrid retrieval lands every relevant doc in the candidate
pool (`recall@50 = 1.000`), and the phase-1 LLM reranker lifted `recall@5` 0.700 → 0.750.
The 5 residual misses all rank too deep to be *seen* by the reranker:

| Query | expected doc | rank | reachable via `related:` |
|---|---|---:|---|
| Safety MCU CAN message IDs and heartbeat frames | `docs/CAN/SAFETY-MCU-MESSAGES.md` | 33 | `MESSAGE_FLOW.md` (top hit) → forward edge to it |
| telemetry timing architecture and rates | `docs/telemetry/*` | 43 | dense bidirectional telemetry cluster |
| VCU connector pinout and connector map | `docs/hardware/vcu/connector-map.md` | 18 | **backlink only** — its own `related:` is empty |
| virtual load cell estimation | (load-cell doc) | 8 | in-pool, below top-5 |
| VCU power architecture | `docs/hardware/vcu/power-architecture.md` | 6 | bidirectional power cluster |

Two of the five (ranks 33, 43) are **outside the rerank candidate pool of 30 entirely** —
the reranker never sees them. Every miss, however, is a 1-hop `related:` neighbour of a
document that *does* rank highly. So the Carta-native lever is: walk the `related:` graph
from the top hits and pull adjacent-but-deep documents up into the pool the reranker judges.

Two facts shape the design:
- **Traversal must be undirected.** `connector-map.md` has an empty `related:` list and is
  reachable *only* as a backlink target; a directed (forward-only) walk would never find it.
- **The `related:` graph is currently disconnected for ~5 entries.** Of 73 docs with
  `related:` frontmatter, entries are overwhelmingly path-style and repo-root-relative
  (`docs/CAN/TOPOLOGY.md`), but a tail is broken: bare ids (`connector-map`,
  `teensy-io-allocation`, `nucleo-g0b1re-io-allocation`), a missing-`docs/`-prefix path, and
  a `.pdf`/`.embed-meta.yaml` target. `build_related_graph` today keys only on `.md` paths
  under `docs/`, so those entries — and root files like `CLAUDE.md` — silently don't connect.

## Goals / Non-goals

**Goals**
- Add **undirected 1-hop graph expansion** to `run_search`: promote `related:`-adjacent
  candidates of the top seeds into the rerank candidate pool.
- **Normalize at search time:** a resolver that maps any `related:` entry style (exact path,
  missing-`docs/`-prefix, bare id/slug, extension drift) to a canonical repo-root path, so the
  graph connects without editing the docs.
- **Audit reports bad entries:** a scanner check flags `related:` entries that resolve only via
  fallback (non-canonical) or not at all, feeding the linking sweep that densifies the graph.
- **On by default, opt-out-able:** ships enabled; the `search.graph.enabled` config knob is the
  opt-out (set `false` for low-memory / old machines). Graph expansion is lightweight (a cached
  frontmatter walk), so no separate interactive `carta init` prompt is added. Fail-open — any
  graph error returns today's behaviour unchanged.
- **Measurable:** re-run `et-embed.yaml` with graph on + LLM rerank on; success = the rank-33
  (Safety MCU) and rank-43 (telemetry) docs reaching top-5, `recall@5` > 0.750.

**Non-goals**
- Auto-rewriting `related:` frontmatter to canonical paths (the audit *reports*; the linking
  sweep cleans). No doc churn in this phase.
- A standalone "boost above fused hits into top-5" knob with no reranker (precision risk; YAGNI).
  Graph's `recall@5` lift is realized **through** the reranker — see below.
- The reranker → link-suggestion flywheel (phase 3).
- Multi-hop traversal, PDF outbound edges (PDFs have no frontmatter), any new external API.

## Design

### Mechanism — undirected 1-hop promotion into the rerank pool

`run_search` (`carta/embed/pipeline.py`) today: hybrid fetch → `_rrf_merge_collections` →
(optional) `rerank_dispatch` → return `top_n`. Graph expansion inserts one step between fusion
and rerank, gated on `search.graph.enabled`:

1. **Deep fetch.** When graph is enabled, fetch `max(candidate_pool, graph.candidate_depth)`
   (default 50) fused candidates instead of `candidate_pool`, so the rank-33/43 docs are present.
2. **Seeds.** Take the top `graph.seed_count` (default 10) fused hits. Map each to a canonical
   path by stripping any ` (page N)` suffix from `source` (visual hits carry it; text hits don't).
3. **Expand.** `walk_hops(seeds, undirected_graph, hops=graph.hops)` (default 1) → set of
   neighbour canonical paths, excluding the seeds themselves.
4. **Promote.** Reorder the deep pool so neighbour candidates move to immediately follow the
   seeds — stable within each group (seeds keep fused order, promoted neighbours keep fused order,
   remainder keeps fused order). Truncate to `candidate_pool`. This **guarantees** graph-adjacent
   deep docs enter the pool the reranker sees.
5. **Rerank as today.** Hand the augmented pool to `rerank_dispatch`. The reranker is the final
   arbiter: a promoted-but-irrelevant neighbour simply ranks low (fail-safe). If rerank is
   disabled, take `top_n` of the promoted ordering.

**Why the lift is realized through the reranker (honest scope).** Because
`seed_count (10) ≥ top_n (5)`, promotion never displaces the top fused hits from positions
1..seed_count — so with rerank *off*, graph expansion only reorders positions 11+ and cannot
change `recall@5`. Its value is feeding the reranker candidates it otherwise never sees
(ranks 33, 43). The measurement run therefore uses LLM rerank on. This is deliberate: it keeps
graph expansion safe (it never demotes a strong hit on its own) and composes with phase 1 rather
than introducing a competing, precision-risky standalone booster.

### Normalization — search-time resolver (`carta/search/graph.py`, extended)

- `build_doc_index(repo_root) -> dict[str, str]` — maps each known doc's frontmatter `id:` slug
  **and** its kebab-cased filename stem to its canonical repo-root POSIX path. Built over all
  `.md` docs including root-level files (`CLAUDE.md`). On collision, exact-path wins; ambiguous
  stems are recorded (and left for the audit to surface), never silently mis-resolved.
- `resolve_entry(entry, doc_index, repo_root) -> str | None` — resolution order:
  1. `entry` as a repo-root-relative path that exists / is a known doc → canonical.
  2. `docs/` + `entry` exists → canonical (missing-prefix case).
  3. kebab-case the stem of `entry` → `doc_index` lookup (handles bare ids like `connector-map`).
  4. extension-normalize (`.embed-meta.yaml`/`.pdf` → the doc's canonical path if known).
  5. else → `None` (unresolved; reported by the audit, dropped from the graph).
- `build_related_graph(repo_root)` (rewrite) — for every `.md` doc (incl. root), parse `related:`,
  `resolve_entry` each, and build an **undirected** adjacency: forward edges ∪ inverted/backlink
  edges. Returns `dict[canonical_path, set[canonical_path]]`. Canonical keys match search hits'
  `source` exactly (repo-root-relative POSIX).
- **Caching.** A module-level memo keyed on `(repo_root, docs-tree max mtime)` so repeated
  searches and the per-prompt proactive-recall hook don't re-read 73 files each call; invalidated
  when any doc's mtime advances.
- `expand_seeds(...)` / `promote_graph_neighbors(pool, neighbours, seed_count)` — pure, unit-tested.

### Config (`search.graph`)

```yaml
search:
  graph:
    enabled: true          # NEW — on by default; carta init offers opt-out
    hops: 1                # NEW — traversal depth
    seed_count: 10         # NEW — how many top fused hits seed the walk
    candidate_depth: 50    # NEW — deep-fetch size when graph is enabled
```

`DEFAULTS["search"]["graph"]` in `carta/config.py` gains these. The knob is the opt-out; no new
`carta init` prompt (graph expansion is lightweight — a cached frontmatter walk, unlike ColPali).

### Audit check (`carta/scanner/scanner.py`)

A new check (e.g. `check_noncanonical_related`) runs `resolve_entry` over every `related:` entry
and emits findings for: (a) entries that resolve only via a fallback tier (non-canonical — e.g.
`connector-map`, missing-`docs/`-prefix) with the suggested canonical path, and (b) entries that
don't resolve at all (broken). These flow into `AUDIT_REPORT.md`/`TRIAGE.md` like other findings,
so the linking sweep can canonicalize them over time. This is the "audit reports, doesn't rewrite"
half of normalization.

### Data flow

`run_search` → hybrid fetch (deep `candidate_depth` when graph on) → `_rrf_merge_collections` →
**graph step** (`enabled`? seeds → `build_related_graph`(cached) → `expand_seeds` →
`promote_graph_neighbors` → truncate to `candidate_pool`) → `rerank_dispatch` → strip transient
keys → return `top_n`. Visual/RRF fusion path otherwise unchanged.

### Error handling (fail-open everywhere)

- Graph build raises / `docs/` missing / no `related:` anywhere → skip expansion, log one stderr
  line, return the fused order.
- A seed `source` that doesn't map to a graph node → contributes no neighbours; others proceed.
- `graph.enabled` true but reranker disabled → promotion reorders, take `top_n` (no `recall@5`
  change by construction, as noted; documented, not an error).

### Testing (TDD)

Pure-function / mocked-Qdrant tests, no live model:
- `resolve_entry`: each tier resolves (exact, missing-prefix, bare-id `connector-map`, extension
  drift); unresolvable → `None`.
- `build_related_graph`: undirected reachability — specifically the **backlink-only**
  `connector-map` case (empty own `related:`, reached from a doc that links to it); root files
  (`CLAUDE.md`) included as nodes.
- `promote_graph_neighbors`: neighbours move to just-after seeds, stable within each group,
  truncates to `candidate_pool`; seeds never displaced.
- `run_search` dispatch: graph on injects a known deep neighbour into the pool handed to rerank;
  graph off → result byte-identical to today; graph build raising → fail-open to fused order.
- Cache: second call within the same mtime window does not re-parse (assert via a parse counter).

Live eval (manual, not CI): `carta eval .carta/eval/et-embed.yaml -k 5` with `graph.enabled: true`
+ `rerank.backend: llm` vs the 0.750 phase-1 baseline. Success = Safety-MCU (was rank 33) and the
telemetry doc (was rank 43) in top-5; `recall@5` > 0.750.

## Open questions (resolve in plan)

1. **`build_doc_index` source** — glob the repo for `.md` (simple, always current) vs read the 374
   sidecars' `current_path`/`slug` (authoritative index of *embedded* docs, incl. PDFs as nodes).
   Lean: glob `.md` for the graph (the related-source is md frontmatter), since PDFs have no
   outbound edges and the recall misses are all `.md`. Revisit if PDF-as-neighbour is wanted.
2. **Cache invalidation granularity** — single max-mtime over the docs tree (cheap, coarse) vs
   per-file. Lean: max-mtime; the per-prompt hook rebuilds at most once per doc edit.

## Phasing

- **Phase 1 (done, v0.8.0):** LLM reranker backend.
- **Phase 2 (this spec):** `related:` resolver + undirected 1-hop expansion + audit check for
  non-canonical entries.
- **Phase 3:** mine reranker signal to *suggest* new `related:` links back into the audit (the flywheel).
