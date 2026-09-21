# HyDE Hypothetical-Answer Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a search caller pass a hypothetical answer that is blended into the dense query vector, on `run_search`, `carta search`, MCP `carta_search`, and the `/doc-search` skill.

**Architecture:** A new focused module `carta/search/hyde.py` owns the prefix, the blend math, and the hypothetical embed. `run_search` and the MCP per-collection search call it after embedding the query; only the dense lane sees the blended vector. Nothing changes when no hypothetical is passed.

**Tech Stack:** Python 3.10+, Ollama `/api/embeddings` (nomic-embed-text), qdrant-client, pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-hyde-hypothetical-query-design.md`

## Global Constraints

- Blend is exactly `unit(unit(embed("search_query: " + query)) + unit(embed("search_document: " + hypothetical)))`.
- Dense lane only. BM25 and the ColPali visual lane keep the raw query string.
- No hypothetical (None, "", whitespace) → behaviour byte-identical to today.
- Carta never generates a hypothetical itself (no new model dependency, no new latency).
- Not wired into the proactive-recall hook.
- Tests: `python3 -m pytest` from the repo root; full suite must stay green.

---

### Task 1: `carta/search/hyde.py` — blend + hypothetical embed

**Files:**
- Create: `carta/search/hyde.py`
- Test: `carta/search/tests/test_hyde.py`

**Interfaces:**
- Produces: `HYPOTHETICAL_PREFIX: str = "search_document: "`; `blend(query_vec: list[float], hyp_vec: list[float]) -> list[float]`; `embed_hypothetical(text: str, ollama_url: str, model: str, timeout: float | None = None) -> list[float]`; `usable(hypothetical: str | None) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
import math
import pytest
from carta.search import hyde

def _norm(v): return math.sqrt(sum(x * x for x in v))

def test_blend_is_unit_norm_equal_weight_mean():
    out = hyde.blend([3.0, 0.0], [0.0, 5.0])
    assert out == pytest.approx([1 / math.sqrt(2), 1 / math.sqrt(2)])
    assert _norm(out) == pytest.approx(1.0)

def test_blend_normalises_inputs_first():
    # magnitude must not decide the mix
    assert hyde.blend([100.0, 0.0], [0.0, 1.0]) == pytest.approx(hyde.blend([1.0, 0.0], [0.0, 1.0]))

def test_blend_identical_vectors_returns_that_direction():
    assert hyde.blend([0.0, 2.0], [0.0, 7.0]) == pytest.approx([0.0, 1.0])

def test_usable():
    assert hyde.usable("a passage")
    assert not hyde.usable(None) and not hyde.usable("") and not hyde.usable("   ")

def test_embed_hypothetical_uses_document_prefix(monkeypatch):
    seen = {}
    def fake(text, **kw):
        seen.update(kw, text=text); return [1.0, 0.0]
    monkeypatch.setattr(hyde, "get_embedding", fake)
    hyde.embed_hypothetical("the bus runs at 500 kbps", "http://o", "m", timeout=2.0)
    assert seen["prefix"] == "search_document: "
    assert seen["text"] == "the bus runs at 500 kbps"
    assert seen["timeout"] == 2.0 and seen["model"] == "m" and seen["ollama_url"] == "http://o"

def test_embed_hypothetical_omits_timeout_when_none(monkeypatch):
    seen = {}
    monkeypatch.setattr(hyde, "get_embedding", lambda text, **kw: seen.update(kw) or [1.0])
    hyde.embed_hypothetical("x", "http://o", "m")
    assert "timeout" not in seen   # requests treats timeout=None as "wait forever"
```

- [ ] **Step 2: Run to verify it fails** — `python3 -m pytest carta/search/tests/test_hyde.py -q` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
"""HyDE — blend a caller-written hypothetical answer into the dense query vector.

A short question and a long passage sit in different regions of embedding space. A
hypothetical answer (it need not be correct) is shaped like the passage, so blending it
in pulls the dense lane toward documents that read like the answer. Measured on the
84-query ET-embed set: +0.24 MRR on the dense-only MCP path with Claude-written
hypotheticals; a local 9b writer did not help — so Carta never writes one itself.
See docs/superpowers/specs/2026-09-21-hyde-hypothetical-query-design.md.
"""
from __future__ import annotations

import math

from carta.embed.embed import get_embedding

HYPOTHETICAL_PREFIX = "search_document: "   # it is shaped like a document, so embed it as one


def usable(hypothetical: str | None) -> bool:
    return bool(hypothetical and hypothetical.strip())


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def blend(query_vec: list[float], hyp_vec: list[float]) -> list[float]:
    """Equal-weight mean of the two unit vectors, renormalised.

    Keeping the query in the vector bounds the downside of an off-topic hypothetical:
    this was the only blend that did not hurt with a weak writer (spec, conclusion 4).
    """
    q, h = _unit(query_vec), _unit(hyp_vec)
    return _unit([a + b for a, b in zip(q, h)])


def embed_hypothetical(text: str, ollama_url: str, model: str,
                       timeout: float | None = None) -> list[float]:
    kwargs = {"ollama_url": ollama_url, "model": model, "prefix": HYPOTHETICAL_PREFIX}
    if timeout is not None:   # None would mean "no ceiling" to requests (issue #106)
        kwargs["timeout"] = timeout
    return get_embedding(text, **kwargs)
```

- [ ] **Step 4: Run to verify it passes** — same command → 6 passed.
- [ ] **Step 5: Commit** — `git add carta/search/hyde.py carta/search/tests/test_hyde.py && git commit -m "feat(search): hyde module — blend a hypothetical answer into the query vector"`

---

### Task 2: `run_search(…, hypothetical=)` and `carta search --hypothetical`

**Files:**
- Modify: `carta/embed/pipeline.py` (`run_search` signature + right after `text_query_vec = _embed_query_or_raise(...)`)
- Modify: `carta/cli.py` (`search_p` args; `cmd_search` call)
- Test: `carta/embed/tests/test_hyde_search.py`, `carta/tests/test_cli.py`

**Interfaces:**
- Consumes: `hyde.usable`, `hyde.embed_hypothetical`, `hyde.blend` (Task 1).
- Produces: `run_search(query, cfg, verbose=False, stats=None, timeout_s=None, *, trace_stages=None, hypothetical: str | None = None)`.

- [ ] **Step 1: Write the failing tests** (`test_hyde_search.py`): patch `find_config`, `get_search_collections` → `["p_doc"]`, `_embed_query_or_raise` → `[1.0, 0.0]`, `hyde.embed_hypothetical` → `[0.0, 1.0]`, `collection_is_hybrid` → True, and `_lane_queries` with a recorder of `(query, dense_vec)` returning `([], [])`. Assert:
  - with `hypothetical="passage"`: recorded dense_vec ≈ `[0.7071, 0.7071]` and recorded query == the raw query;
  - without it: dense_vec == `[1.0, 0.0]` and `embed_hypothetical` never called;
  - with `hypothetical="   "`: same as without;
  - the hypothetical embed receives the budget (`timeout_s=5` → a float ≤ 5 is passed).
  CLI (`test_cli.py`): `carta search foo --hypothetical "bar baz"` parses and `cmd_search` calls `run_search(..., hypothetical="bar baz")`.

- [ ] **Step 2: Run to verify they fail** — `python3 -m pytest carta/embed/tests/test_hyde_search.py carta/tests/test_cli.py -q -k "hyde or hypothetical"` → TypeError (unexpected kwarg) / AttributeError.

- [ ] **Step 3: Implement** — in `run_search`, keyword-only `hypothetical: str | None = None`; after the query embed:

```python
    if text_query_vec is not None and hyde.usable(hypothetical):
        # Dense lane only: BM25 and ColPali keep the raw query (spec: Design/Lanes).
        try:
            hyp_vec = hyde.embed_hypothetical(
                hypothetical, cfg["embed"]["ollama_url"], cfg["embed"]["ollama_model"],
                timeout=_remaining())
        except Exception as e:
            raise RuntimeError(f"Could not embed the hypothetical — run: carta doctor\n(Detail: {e})") from e
        text_query_vec = hyde.blend(text_query_vec, hyp_vec)
```

  CLI: `search_p.add_argument("--hypothetical", metavar="TEXT", help=...)`; `cmd_search` passes `hypothetical=getattr(args, "hypothetical", None)`.

- [ ] **Step 4: Run to verify they pass**, then the full suite.
- [ ] **Step 5: Commit** — `feat(search): run_search and carta search accept a hypothetical answer (HyDE)`

---

### Task 3: MCP `carta_search(…, hypothetical=)`

**Files:**
- Modify: `carta/mcp/server.py` (`carta_search` signature + docstring; `_run_search_collection(..., hypothetical=None)`)
- Test: `carta/mcp/tests/test_server.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `carta_search(query, top_k=5, scope="repo", hypothetical: str | None = None)`; `_run_search_collection(query, cfg, collection_name, top_n, hypothetical=None)`.

- [ ] **Step 1: Failing tests:** (a) `carta_search("q", hypothetical="h")` forwards `hypothetical="h"` to `_run_search_collection` (record kwargs via a `lambda *a, **k`); (b) `_run_search_collection` with a `ContractFakeQdrant` (from `carta/mcp/tests/fakes.py`), `get_embedding` → `[1.0, 0.0]` and `hyde.embed_hypothetical` → `[0.0, 1.0]`: the `query` sent to `query_points` ≈ `[0.7071, 0.7071]`; without a hypothetical it is `[1.0, 0.0]`; (c) a failing hypothetical embed raises `QueryEmbeddingError` (surfaced by `carta_search` as `{"error": "embedding_unavailable"}`).
- [ ] **Step 2: Verify they fail.**
- [ ] **Step 3: Implement** the blend right after `query_vec = get_embedding(...)` inside the existing try, wrapping failure in `QueryEmbeddingError`; forward `hypothetical` from `carta_search`. Docstring (the agent-facing tool description) gains:

```
        hypothetical: Optional. 2-5 sentences written the way this project's own docs
            would state the answer — your best guess; it does NOT need to be correct.
            Blended into the semantic match, it finds documents phrased differently
            from the question (measured: +0.24 MRR, recall@5 0.54 -> 0.77). This tool's
            text search is semantic only; the visual lane still uses `query`.
```

- [ ] **Step 4: Verify pass + full suite.**
- [ ] **Step 5: Commit** — `feat(mcp): carta_search accepts a hypothetical answer (HyDE)`

---

### Task 4: Skill, docs, and end-to-end verification

**Files:**
- Modify: `carta/skills/doc-search/SKILL.md` (Step 2), `CLAUDE.md` (surface table: `search` row + MCP line), `README.md`, `CHANGELOG.md` (`[Unreleased]` → Added), `docs/ROADMAP.md` (rationale: HyDE shipped caller-side; local-writer HyDE and the negative anchor rejected, with numbers).

- [ ] **Step 1:** `/doc-search` Step 2 — write a 2–5 sentence hypothetical passage phrased as the project's docs would state the answer, then run `carta search "<query>" --hypothetical "<passage>"`.
- [ ] **Step 2:** Docs as listed. No eval-set query text in this public repo — aggregate numbers only.
- [ ] **Step 3: End-to-end verification (not committed):** run the 84 ET-embed queries through the *shipped* `run_search(..., hypothetical=h)` and `carta_search(..., top_k=10, hypothetical=h)` with the Claude-ctx hypotheticals, `top_n=10`. Expected: CLI MRR 0.650, MCP MRR 0.698 — identical to the benchmark's `hyde_claude_ctx:mean` (retrieval is deterministic). A mismatch means the shipped code is not what was measured.
- [ ] **Step 4:** Full suite green; commit `docs: HyDE — skill, surface table, rationale`.
