---
id: 2026-06-09-llm-reranker
title: "LLM Reranker Backend — Implementation Plan"
status: shipped
related:
  - 2026-06-09-llm-reranker-design
date: 2026-06-09
---

# LLM Reranker Backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `search.rerank.backend: llm` that reorders the hybrid candidate pool with a single listwise Ollama call, leaving the existing cross-encoder path and default behavior unchanged.

**Architecture:** A new pure-ish unit `carta/search/llm_rerank.py` (one Ollama `/api/chat` call, parse a JSON index order, reorder + truncate, fail-open). A small `rerank_dispatch()` in `carta/search/rerank.py` routes on `backend`. `run_search` calls the dispatcher instead of `rerank_hits` directly. Config gains `backend`/`llm_model`/`llm_timeout_s`.

**Tech Stack:** Python 3.10+, `requests` (already a dep), Ollama `/api/chat`, pytest. Spec: `docs/superpowers/specs/2026-06-09-llm-reranker-design.md`.

---

## File Structure

- **Create** `carta/search/llm_rerank.py` — `llm_rerank_hits()` + helpers. One job: LLM listwise rerank, fail-open.
- **Create** `carta/search/tests/test_llm_rerank.py` — unit tests, mocked `requests.post` (no live Ollama).
- **Modify** `carta/search/rerank.py` — add `rerank_dispatch()` (routes cross-encoder | llm). Existing `rerank_hits` untouched.
- **Modify** `carta/search/tests/test_rerank.py` — tests for `rerank_dispatch` routing.
- **Modify** `carta/config.py` — `DEFAULTS["search"]["rerank"]` gains `backend`, `llm_model`, `llm_timeout_s`.
- **Modify** `carta/tests/test_config.py` — assert new rerank defaults.
- **Modify** `carta/embed/pipeline.py` — `run_search` rerank block calls `rerank_dispatch`.
- **Modify** `README.md`, `CHANGELOG.md`, version files — docs + 0.8.0 release (final task).

---

## Task 1: `llm_rerank_hits` — happy-path reorder + truncate

**Files:**
- Create: `carta/search/llm_rerank.py`
- Test: `carta/search/tests/test_llm_rerank.py`

- [ ] **Step 1: Write the failing test**

```python
# carta/search/tests/test_llm_rerank.py
import json
from unittest.mock import patch, MagicMock
from carta.search.llm_rerank import llm_rerank_hits


def _hits():
    return [
        {"source": "a.md", "excerpt": "alpha", "type": "text"},
        {"source": "b.md", "excerpt": "bravo", "type": "text"},
        {"source": "c.md", "excerpt": "charlie", "type": "text"},
        {"source": "d.md", "excerpt": "delta", "type": "text"},
    ]


def _resp(content: str):
    r = MagicMock()
    r.json.return_value = {"message": {"content": content}}
    r.raise_for_status.return_value = None
    return r


def test_reorders_to_llm_index_order_and_truncates():
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("[2, 0, 1, 3]")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["c.md", "a.md"]
    assert out[0]["rerank_score"] > out[1]["rerank_score"]


def test_accepts_object_wrapped_array():
    # Ollama format=json sometimes wraps: {"order": [...]}
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp('{"order": [1, 0]}')):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["b.md", "a.md"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/search/tests/test_llm_rerank.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'carta.search.llm_rerank'`

- [ ] **Step 3: Write minimal implementation**

```python
# carta/search/llm_rerank.py
"""Listwise LLM reranker via a single Ollama /api/chat call.

Reorders the fused candidate pool by LLM-judged relevance to the query and
truncates to top_n. Fail-open: any error/timeout/parse failure returns the
input order unchanged (never worse than no rerank). Local Ollama only.
"""
from __future__ import annotations

import json
import sys
import requests

_SYSTEM = (
    "You rank document passages by relevance to a search query. "
    "Return ONLY a JSON array of passage numbers, most relevant first. "
    "Include only clearly relevant passages."
)


def _build_prompt(query: str, hits: list[dict], max_excerpt_chars: int) -> str:
    lines = [f"Query: {query}", "", "Passages:"]
    for i, h in enumerate(hits):
        excerpt = (h.get("excerpt", "") or "")[:max_excerpt_chars].replace("\n", " ")
        lines.append(f"[{i}] {h.get('source', '')}: {excerpt}")
    lines.append("")
    lines.append("Return a JSON array of the passage numbers, most relevant first.")
    return "\n".join(lines)


def _parse_order(content: str, n: int) -> list[int]:
    """Parse the model reply into a de-duplicated list of valid in-range indices."""
    data = json.loads(content)
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    order: list[int] = []
    seen = set()
    for x in data:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in seen:
            seen.add(i)
            order.append(i)
    return order


def llm_rerank_hits(query: str, hits: list[dict], *, model: str, ollama_url: str,
                    top_n: int, timeout_s: int = 20, max_excerpt_chars: int = 500) -> list[dict]:
    """Reorder *hits* by a single listwise Ollama call; return top_n. Fail-open."""
    if not hits or not query.strip():
        return hits[:top_n]
    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _build_prompt(query, hits, max_excerpt_chars)},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        order = _parse_order(content, len(hits))
    except Exception as exc:  # fail-open — never worse than the fused order
        print(f"llm_rerank: fail-open ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
        return hits[:top_n]

    if not order:
        return hits[:top_n]
    # Ranked first (LLM order), then any unranked in original fused order.
    ranked = [hits[i] for i in order]
    remainder = [h for j, h in enumerate(hits) if j not in set(order)]
    merged = ranked + remainder
    for rank, h in enumerate(merged):
        h["rerank_score"] = float(len(merged) - rank)
    return merged[:top_n]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/search/tests/test_llm_rerank.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add carta/search/llm_rerank.py carta/search/tests/test_llm_rerank.py
git commit -m "feat(search): listwise LLM reranker core (llm_rerank_hits)"
```

---

## Task 2: Fail-open behavior

**Files:**
- Modify: `carta/search/tests/test_llm_rerank.py`
- (implementation already handles these — this task proves it)

- [ ] **Step 1: Write the failing tests**

```python
# append to carta/search/tests/test_llm_rerank.py
import requests as _requests


def test_fail_open_on_timeout():
    with patch("carta.search.llm_rerank.requests.post", side_effect=_requests.Timeout()):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=3)
    assert [h["source"] for h in out] == ["a.md", "b.md", "c.md"]  # original order, top_n


def test_fail_open_on_non_json():
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("not json at all")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["a.md", "b.md"]


def test_out_of_range_and_dupes_filtered_then_filled():
    # indices 9 (oob) and duplicate 0 dropped; remainder filled from fused order
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("[3, 9, 3]")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=4)
    assert [h["source"] for h in out] == ["d.md", "a.md", "b.md", "c.md"]


def test_empty_hits_and_blank_query():
    assert llm_rerank_hits("q", [], model="m", ollama_url="u", top_n=5) == []
    out = llm_rerank_hits("   ", _hits(), model="m", ollama_url="u", top_n=2)
    assert [h["source"] for h in out] == ["a.md", "b.md"]  # no LLM call on blank query
```

- [ ] **Step 2: Run tests to verify they pass** (implementation from Task 1 already covers these)

Run: `python -m pytest carta/search/tests/test_llm_rerank.py -q`
Expected: PASS (6 passed). If any fail, fix `llm_rerank.py` to match the asserted fail-open behavior.

- [ ] **Step 3: Commit**

```bash
git add carta/search/tests/test_llm_rerank.py
git commit -m "test(search): cover llm_rerank fail-open paths"
```

---

## Task 3: Config defaults

**Files:**
- Modify: `carta/config.py` (the `DEFAULTS["search"]["rerank"]` dict)
- Test: `carta/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# append to carta/tests/test_config.py
def test_rerank_backend_defaults():
    from carta.config import DEFAULTS
    rr = DEFAULTS["search"]["rerank"]
    assert rr["backend"] == "cross-encoder"        # default unchanged behavior
    assert rr["llm_model"] == "qwen3.5:0.8b"
    assert rr["llm_timeout_s"] == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/tests/test_config.py::test_rerank_backend_defaults -q`
Expected: FAIL — `KeyError: 'backend'`

- [ ] **Step 3: Edit `carta/config.py`**

Find the `"rerank"` block inside `DEFAULTS["search"]`:

```python
        "rerank": {
            "enabled": False,
            "model": "BAAI/bge-reranker-base",
            "candidate_pool": 30,
        },
```

Replace with:

```python
        "rerank": {
            "enabled": False,
            "backend": "cross-encoder",   # cross-encoder | llm
            "model": "BAAI/bge-reranker-base",   # used when backend=cross-encoder
            "llm_model": "qwen3.5:0.8b",  # used when backend=llm (local Ollama)
            "llm_timeout_s": 20,
            "candidate_pool": 30,
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/tests/test_config.py::test_rerank_backend_defaults -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/tests/test_config.py
git commit -m "feat(config): rerank backend/llm_model/llm_timeout_s defaults"
```

---

## Task 4: `rerank_dispatch` — backend routing

**Files:**
- Modify: `carta/search/rerank.py` (add `rerank_dispatch`; leave `rerank_hits` as-is)
- Test: `carta/search/tests/test_rerank.py`

- [ ] **Step 1: Write the failing test**

```python
# append to carta/search/tests/test_rerank.py
from unittest.mock import patch
from carta.search.rerank import rerank_dispatch


def _hits():
    return [{"source": "a.md", "excerpt": "x", "type": "text"} for _ in range(3)]


def test_dispatch_routes_to_cross_encoder_by_default():
    rr = {"backend": "cross-encoder", "model": "BAAI/bge-reranker-base"}
    with patch("carta.search.rerank.rerank_hits", return_value=["CE"]) as ce, \
         patch("carta.search.llm_rerank.llm_rerank_hits", return_value=["LLM"]) as llm:
        out = rerank_dispatch("q", _hits(), rr_cfg=rr, ollama_url="u", top_n=2)
    assert out == ["CE"]
    ce.assert_called_once()
    llm.assert_not_called()


def test_dispatch_routes_to_llm_when_backend_llm():
    rr = {"backend": "llm", "llm_model": "qwen3.5:0.8b", "llm_timeout_s": 9}
    with patch("carta.search.rerank.rerank_hits", return_value=["CE"]) as ce, \
         patch("carta.search.llm_rerank.llm_rerank_hits", return_value=["LLM"]) as llm:
        out = rerank_dispatch("q", _hits(), rr_cfg=rr, ollama_url="u", top_n=2)
    assert out == ["LLM"]
    llm.assert_called_once()
    ce.assert_not_called()
    # forwards llm_model + timeout
    assert llm.call_args.kwargs["model"] == "qwen3.5:0.8b"
    assert llm.call_args.kwargs["timeout_s"] == 9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/search/tests/test_rerank.py -k dispatch -q`
Expected: FAIL — `ImportError: cannot import name 'rerank_dispatch'`

- [ ] **Step 3: Add `rerank_dispatch` to `carta/search/rerank.py`**

Append:

```python
def rerank_dispatch(query: str, hits: list[dict], *, rr_cfg: dict, ollama_url: str,
                    top_n: int) -> list[dict]:
    """Route reranking to the configured backend. Defaults to cross-encoder.

    rr_cfg is cfg["search"]["rerank"]. backend="llm" uses the listwise Ollama
    reranker; anything else (incl. unset) uses the fastembed cross-encoder.
    """
    if rr_cfg.get("backend", "cross-encoder") == "llm":
        from carta.search.llm_rerank import llm_rerank_hits
        return llm_rerank_hits(
            query, hits,
            model=rr_cfg.get("llm_model", "qwen3.5:0.8b"),
            ollama_url=ollama_url,
            top_n=top_n,
            timeout_s=rr_cfg.get("llm_timeout_s", 20),
        )
    return rerank_hits(query, hits, model_name=rr_cfg.get("model", DEFAULT_RERANK_MODEL), top_n=top_n)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/search/tests/test_rerank.py -k dispatch -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add carta/search/rerank.py carta/search/tests/test_rerank.py
git commit -m "feat(search): rerank_dispatch routes cross-encoder|llm"
```

---

## Task 5: Wire `run_search` to the dispatcher

**Files:**
- Modify: `carta/embed/pipeline.py` (the rerank block inside `run_search`)

- [ ] **Step 1: Locate the current rerank block**

Run: `grep -n "rerank_hits\|from carta.search.rerank" carta/embed/pipeline.py`
You will find an import `from carta.search.rerank import rerank_hits` and a block like:

```python
    if rerank_enabled and all_results:
        from carta.search.rerank import rerank_hits
        pool = all_results[:candidate_pool]
        for h in pool:
            h["text"] = h.get("excerpt", "")
        all_results = rerank_hits(
            query,
            pool,
            model_name=rr_cfg.get("model", "BAAI/bge-reranker-base"),
            top_n=top_n,
        )
        for _h in all_results:
            _h.pop("text", None)
            _h.pop("rerank_score", None)
```

- [ ] **Step 2: Replace the `rerank_hits(...)` call with the dispatcher**

Change the import line to `from carta.search.rerank import rerank_dispatch` and the call to:

```python
        all_results = rerank_dispatch(
            query,
            pool,
            rr_cfg=rr_cfg,
            ollama_url=cfg.get("embed", {}).get("ollama_url", "http://localhost:11434"),
            top_n=top_n,
        )
```

Leave the `pool`/`text`-stamping and the post-loop `pop` cleanup exactly as they are. (The cross-encoder path reads `h["text"]`; the LLM path reads `h["excerpt"]` — both keys are present, so no change needed there.)

- [ ] **Step 3: Run the full search/pipeline + rerank suites to verify no regression**

Run: `python -m pytest carta/embed/tests/ carta/search/tests/ carta/tests/test_pipeline.py -q`
Expected: PASS (the 2 pre-existing pass; nothing new red). The existing rerank-enabled run_search tests now flow through `rerank_dispatch` → cross-encoder (default backend) → identical behavior.

- [ ] **Step 4: Commit**

```bash
git add carta/embed/pipeline.py
git commit -m "feat(search): run_search reranks via rerank_dispatch (backend-aware)"
```

---

## Task 6: Live eval + model bench (manual — not CI)

**Files:** none (measurement). Requires Qdrant + Ollama up, `qwen3.5:0.8b` + `qwen3.5:9b` pulled, cwd = an embedded project (ET-embed).

- [ ] **Step 1: Baseline (record current)**

Run (cwd ET-embed): `carta eval .carta/eval/et-embed.yaml -k 5`
Record recall@5 / MRR (expected ~0.700 / 0.546 with rerank off).

- [ ] **Step 2: Eval with `backend: llm`, small model**

Temporarily set in `.carta/config.yaml`:
```yaml
search:
  rerank: {enabled: true, backend: llm, llm_model: qwen3.5:0.8b, candidate_pool: 40}
```
Run: `OMP_NUM_THREADS=1 carta eval .carta/eval/et-embed.yaml -k 5`
Record recall@5 / MRR. Success bar: > 0.700 (pulls rank-8–43 docs into top-5).

- [ ] **Step 3: Eval with `llm_model: qwen3.5:9b`**

Switch `llm_model: qwen3.5:9b`, re-run. Record recall@5 / MRR + rough latency.

- [ ] **Step 4: Decide + restore**

Keep `qwen3.5:0.8b` unless 9b is *significantly* better. Restore `.carta/config.yaml` (rerank off / project default). Write the numbers into the PR description.

- [ ] **Step 5: (no commit — measurement only)**

---

## Task 7: Docs + 0.8.0 release

**Files:**
- Modify: `README.md` (Configuration / rerank section)
- Modify: `CHANGELOG.md`, `carta/__init__.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`

- [ ] **Step 1: README — document the backend**

In the rerank/Configuration section, add:

```markdown
- `search.rerank.backend`: `cross-encoder` (default, fastembed `bge-reranker-base`) or `llm`
  (listwise rerank via a single local Ollama call, `search.rerank.llm_model`, default
  `qwen3.5:0.8b`). The LLM backend is fail-open — any error/timeout falls back to the fused order.
```

- [ ] **Step 2: CHANGELOG — add 0.8.0 entry** (above 0.7.1)

```markdown
## [0.8.0] — 2026-06-09

### Added
- **LLM reranker backend** for `search.rerank` (`backend: llm`) — a single listwise Ollama call
  reorders the candidate pool (`llm_model`, default `qwen3.5:0.8b`; `llm_timeout_s`). Opt-in;
  the default cross-encoder path is unchanged. Fail-open: any error/timeout returns the fused order.
```

- [ ] **Step 3: Bump version to 0.8.0** in `carta/__init__.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` (2 occurrences).

- [ ] **Step 4: Full suite + commit**

Run: `python -m pytest -q`
Expected: PASS (all green, 2 skipped).

```bash
git add -A
git commit -m "docs+release: 0.8.0 — LLM reranker backend"
```

- [ ] **Step 5: PR**

Push `feat/llm-reranker`, open a PR to main with the eval numbers from Task 6. After merge, tag `v0.8.0` to publish (release.yml).

---

## Self-Review

- **Spec coverage:** backend dispatch (T4/T5), listwise single call (T1), fail-open (T1/T2), config keys (T3), eval/bench incl. model decision (T6), README + CHANGELOG (T7), candidate_pool open-question resolved to 40 in T6. ✔
- **Placeholders:** none — every code/command step is concrete. ✔
- **Type consistency:** `llm_rerank_hits(query, hits, *, model, ollama_url, top_n, timeout_s, max_excerpt_chars)` and `rerank_dispatch(query, hits, *, rr_cfg, ollama_url, top_n)` are used identically in T4/T5. `rerank_score` stamped (matches the cross-encoder shape `run_search` already strips). ✔
- **Out of scope (correct):** graph-aware retrieval, `related:` normalization, link-discovery flywheel — phase 2/3.
