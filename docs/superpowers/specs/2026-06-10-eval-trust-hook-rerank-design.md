---
id: 2026-06-10-eval-trust-hook-rerank-design
title: "Eval Trust + Hook Rerank Decoupling (v0.9.1) — Design"
status: shipped
related:
  - 2026-06-10-eval-trust-hook-rerank
date: 2026-06-10
---

# Eval Trust + Hook Rerank Decoupling (v0.9.1) — Design

**Date:** 2026-06-10
**Status:** Approved
**Target version:** 0.9.1 (fix/hardening release)

## Problem

1. **The eval harness cannot detect a silently broken reranker.** The 0.8.0 LLM
   reranker shipped fully fail-open (reasoning models answered into
   `message.thinking`, content came back empty) and the eval reported its numbers
   as a win. Both rerankers stamp `rerank_score` on hits only when they actually
   ran — every fail-open path returns unstamped hits — but `run_search` strips
   the key before returning (pipeline.py ~1665), so no caller can tell
   rerank-ran from fail-open.

2. **The hook pays reranker latency on every prompt.** `carta-hook` forces
   `colpali_enabled` off (hook.py:79) but passes `search.rerank` through
   untouched. With `search.rerank.backend: llm` enabled, every prompt submission
   would block on an Ollama rerank call (~10–15 s with a strong model). This
   prevents enabling the now-fixed LLM reranker for explicit `carta search`.

3. **CI actions run on deprecated Node.js 20.** GitHub force-migrates to
   Node 24 on 2026-06-16. `actions/checkout@v4` and `actions/setup-python@v5`
   appear in both workflows.

## Design

### 1. Rerank observability in `run_search` (carta/embed/pipeline.py)

New optional out-param: `run_search(query, cfg, verbose=False, stats=None)`.
When `stats` is a dict, `run_search` records:

- `stats["rerank_requested"]`: bool — `search.rerank.enabled` was true.
- `stats["rerank_applied"]`: bool — `rerank_score` present on returned hits,
  checked **after** `rerank_dispatch` and **before** the key is stripped.

Default `None` → zero behavior change for all existing callers (hook, MCP,
`cmd_search`). Return type unchanged (chosen over a tuple return or stamping a
`reranked` key on every hit: only option with zero blast radius on the four
existing call sites).

The detection signal is structural: both backends stamp `rerank_score` only on
success. LLM fail-open (exception, timeout, empty/unparseable reply, empty
order) returns unstamped hits — exactly the 0.8.0 failure signature.

### 2. Eval assertion (carta/cli.py::cmd_eval)

`cmd_eval` passes a fresh stats dict per query and aggregates:

- Output gains one line:
  - rerank requested: `rerank: applied on 18/20 queries`
  - rerank not requested: `rerank: not requested`
- **Hard fail:** rerank requested but applied on **zero** queries → explicit
  error to stderr, `sys.exit(1)`. A silent fail-open can never masquerade as an
  eval result again. Partial fail-opens surface via the count (no hard fail —
  individual queries may legitimately fail open on timeouts).

`carta/eval/harness.py` stays pure (rank metrics only; it has no config
access). The assertion lives at the CLI layer where config is known.

### 3. Hook forces rerank off (carta/hook/hook.py)

Extend the existing `search_cfg` override to also force
`search.rerank.enabled = False` (deep-copy of the `search`/`rerank` subdicts,
mirroring the colpali pattern). Hard-forced, not configurable — the hook blocks
prompt submission and its three-zone judge already does relevance filtering.
Comment mirrors the ColPali rationale.

### 4. Eval set expansion (ET-embed repo — data, not shipped code)

Grow `~/School/Elementrailer/ET-embed/.carta/eval/et-embed.yaml` from 20 to
~60 queries:

- Grounded in actually-embedded docs (each `expect` substring verified against
  sidecar/Qdrant-payload file paths).
- Phrased as natural questions, not title echoes.
- Cover the supplier/manual breadth alongside existing CAN/telemetry/VCU areas.
- Validate by running the live eval with the new code (exercises the assertion
  for real); capture baseline (rerank off) and reranked numbers.

### 5. CI Node 24 bump

`actions/checkout@v4 → v5`, `actions/setup-python@v5 → v6` in
`.github/workflows/test.yml` and `release.yml`.

### 6. Docs

- CHANGELOG entry for 0.9.1.
- README: hook guarantee (never reranks, never loads ColPali) + new eval
  output line and zero-applied failure mode.

## Testing

TDD per component:

- **pipeline**: `run_search` with stats dict reports `rerank_applied=True` when
  hits come back stamped; `False` when the reranker fails open (unstamped);
  `rerank_requested=False` and `rerank_applied=False` when rerank disabled;
  `stats=None` unchanged behavior.
- **hook**: cfg passed to `run_search` has `search.rerank.enabled == False`
  even when project config enables it (mock capture, mirroring the existing
  colpali-off test).
- **cmd_eval**: zero-applied + requested → exit 1 with clear error; partial
  applied → reports count, exit 0; not requested → `rerank: not requested`.

## Out of scope

- Reranker speed work (model benchmarking, caching).
- Session/quirk capture, chunking, graph flywheel (next cycles).
- Per-query rerank diagnostics in harness metrics dict beyond the CLI line.
