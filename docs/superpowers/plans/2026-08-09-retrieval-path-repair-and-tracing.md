# Retrieval Path Repair and Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the two silent retrieval bugs (MCP search returning empty, the recall gate reading a meaningless score) and add the per-stage trace that makes this class of bug detectable.

**Architecture:** Move dense+sparse fusion from Qdrant server-side into Python so per-lane ranks are available, in a deliberately behaviour-preserving first step (`k=2`, parity-asserted). Write the cross-collection fused score and its contributing ranks onto each hit so every consumer reads the number that actually ranked it. Add one tracing helper with two consumers: an always-on JSONL log for the hook, and a `--trace` flag for CLI error analysis. Replace the hook's absolute-score gate with rank-and-lane-agreement.

**Tech Stack:** Python 3.10+, qdrant-client, pytest, unittest.mock. No new dependencies.

## Global Constraints

- Python 3.10+ syntax; 4-space indent; type hints on new function signatures.
- No new third-party dependencies.
- The hook must **fail open on every path** — every error exits 0 and lets the prompt through.
- Instrumentation must never break search. Every trace write is wrapped and its failure swallowed.
- Legacy unnamed-vector collections (e.g. `Elementrailer_doc`) must keep working.
- Retrieval **ordering must not change anywhere in this plan**. Every task is behaviour-preserving, Task 4 included: it makes `rrf_k` configurable but leaves the default at `2`. Changing the default to 60 is explicitly out of scope and belongs with the eval work. A task that alters result ordering has failed its spec.
- Run the full suite with `python -m pytest carta/ -q` before each commit.
- Existing test style: `unittest.mock.MagicMock` + `monkeypatch`, fakes defined inline in the test module.

## File Structure

| File | Responsibility |
|---|---|
| `carta/mcp/server.py` (modify) | Pass `using=` on named-vector collections; stop swallowing query failures |
| `carta/mcp/tests/fakes.py` (create) | Contract-faithful Qdrant double that rejects unnamed queries against named-vector collections |
| `carta/mcp/tests/test_server.py` (modify) | Regression tests using the new fake |
| `carta/embed/pipeline.py` (modify) | Client-side lane queries + fusion; write fused score and ranks onto hits; `rrf_k` config |
| `carta/search/trace.py` (create) | The tracing primitive — record construction and JSONL append. New module; keeps `pipeline.py` from growing |
| `carta/search/tests/test_trace.py` (create) | Trace record shape and failure-swallowing |
| `carta/hook/hook.py` (modify) | Rank+agreement gate; emit trace records |
| `carta/cli.py` (modify) | `--trace` flag on `carta search` |

---

### Task 1: MCP search passes `using=`, and query failures stop being silent

**Files:**
- Modify: `carta/mcp/server.py:224-270` (`_run_search_collection`), `carta/mcp/server.py:128-135` (the swallow)
- Create: `carta/mcp/tests/fakes.py`
- Test: `carta/mcp/tests/test_server.py`

**Interfaces:**
- Consumes: `collection_is_hybrid`, `DENSE_VECTOR_NAME` from `carta.embed.embed`
- Produces: `CollectionMissing` and `QdrantQueryError` exception classes in `carta/mcp/server.py`; `ContractFakeQdrant` in `carta/mcp/tests/fakes.py`

- [ ] **Step 1: Write the contract-faithful fake**

The bug shipped because every MCP test patched the Qdrant client with a permissive mock. This fake encodes the real contract.

```python
# carta/mcp/tests/fakes.py
"""Qdrant test doubles that enforce the real server's contract.

The MCP carta_search bug (empty results on every hybrid collection) survived
because the existing fakes accepted any query_points() call. Real Qdrant
returns 400 "Not existing vector name error" when a collection has named
vectors and the query omits `using=`. This double does the same.
"""
from unittest.mock import MagicMock


class NamedVectorContractError(Exception):
    """Stand-in for Qdrant's 400 on an unnamed query against named vectors."""


class ContractFakeQdrant:
    def __init__(self, named_vectors: bool = True, points=None):
        self._named = named_vectors
        self._points = points if points is not None else []
        self.calls: list[dict] = []

    def get_collection(self, name):
        info = MagicMock()
        if self._named:
            info.config.params.vectors = {"dense": MagicMock()}
            info.config.params.sparse_vectors = {"bm25": MagicMock()}
        else:
            info.config.params.vectors = MagicMock()   # unnamed single vector
            info.config.params.sparse_vectors = None
        return info

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        if self._named and "using" not in kwargs and "prefetch" not in kwargs:
            raise NamedVectorContractError(
                "Wrong input: Not existing vector name error: "
            )
        resp = MagicMock()
        resp.points = self._points
        return resp
```

- [ ] **Step 2: Write the failing tests**

```python
# carta/mcp/tests/test_server.py  (append)
import pytest
from carta.mcp import server
from carta.mcp.tests.fakes import ContractFakeQdrant, NamedVectorContractError


def _cfg():
    return {
        "project_name": "p",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434",
                  "ollama_model": "nomic-embed-text:latest"},
    }


def _point(path="docs/a.md", text="hello"):
    p = MagicMock()
    p.score = 0.9
    p.payload = {"file_path": path, "text": text}
    return p


def test_search_named_vector_collection_returns_hits(monkeypatch):
    fake = ContractFakeQdrant(named_vectors=True, points=[_point()])
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    hits = server._run_search_collection("q", _cfg(), "ET-embed_doc", 5)

    assert len(hits) == 1
    assert fake.calls[-1]["using"] == "dense"


def test_search_legacy_unnamed_collection_omits_using(monkeypatch):
    fake = ContractFakeQdrant(named_vectors=False, points=[_point()])
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    hits = server._run_search_collection("q", _cfg(), "Elementrailer_doc", 5)

    assert len(hits) == 1
    assert "using" not in fake.calls[-1]


def test_query_failure_propagates_and_is_not_swallowed(monkeypatch):
    class Exploding(ContractFakeQdrant):
        def query_points(self, **kwargs):
            raise RuntimeError("boom")

    fake = Exploding(named_vectors=True)
    monkeypatch.setattr(server, "QdrantClient", lambda **kw: fake)
    monkeypatch.setattr(server, "get_embedding", lambda *a, **k: [0.0] * 768)

    with pytest.raises(server.QdrantQueryError):
        server._run_search_collection("q", _cfg(), "ET-embed_doc", 5)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest carta/mcp/tests/test_server.py -k "named_vector or unnamed or propagates" -v`
Expected: FAIL — first test raises `NamedVectorContractError` (this is the live bug, reproduced); third fails with `AttributeError: module 'carta.mcp.server' has no attribute 'QdrantQueryError'`.

- [ ] **Step 4: Add the exception types**

```python
# carta/mcp/server.py  (near QueryEmbeddingError)
class CollectionMissing(Exception):
    """The collection does not exist. Safe to skip — other collections may still answer."""


class QdrantQueryError(Exception):
    """The query itself failed. NOT safe to skip — it is identical for every collection."""
```

- [ ] **Step 5: Fix the query and the error handling**

Replace the `client.query_points(...)` block in `_run_search_collection`:

```python
    from carta.embed.embed import DENSE_VECTOR_NAME, collection_is_hybrid

    query_kwargs = {
        "collection_name": collection_name,
        "query": query_vec,
        "limit": top_n,
        "with_payload": True,
    }
    # Named-vector (hybrid-schema) collections REQUIRE `using=`; Qdrant 400s without
    # it. Legacy unnamed-dense collections reject `using=`, so it must be omitted.
    if collection_is_hybrid(client, collection_name):
        query_kwargs["using"] = DENSE_VECTOR_NAME

    from qdrant_client.http.exceptions import UnexpectedResponse

    try:
        response = client.query_points(**query_kwargs)
    except UnexpectedResponse as e:
        # Classify on status_code, NOT on message text. UnexpectedResponse.__str__
        # embeds up to 200 bytes of the raw HTTP body, so a 500 or a proxy error
        # page whose body happens to contain "not found" would be misread as a
        # missing collection and silently skipped — reintroducing the exact
        # swallow this task exists to remove. Mirrors collection_is_hybrid
        # (carta/embed/embed.py:179-181).
        if getattr(e, "status_code", None) == 404:
            raise CollectionMissing(collection_name) from e
        raise QdrantQueryError(
            f"Qdrant search failed for {collection_name}: {e}"
        ) from e
    except Exception as e:
        # Non-UnexpectedResponse transports may carry no status_code; fall back to
        # message text for those only.
        err = str(e).lower()
        if "404" in err or "not found" in err or "doesn't exist" in err:
            raise CollectionMissing(collection_name) from e
        raise QdrantQueryError(
            f"Qdrant search failed for {collection_name}: {e}"
        ) from e
```

**Ruling (2026-08-10):** an earlier draft of this step classified on message substring alone. The Task 1 review flagged it as Important and the human partner ruled the plan wrong; the snippet above is the governing version. Task 1's tests must include a regression case: `UnexpectedResponse(status_code=500)` whose body text contains "not found" must raise `QdrantQueryError`, not `CollectionMissing`.

- [ ] **Step 6: Narrow the swallow at the call site**

At `carta/mcp/server.py:133-135`, replace `except RuntimeError: pass` with:

```python
            except CollectionMissing:
                # This collection does not exist yet — other collections may still answer.
                continue
```

`QdrantQueryError` now propagates to `carta_search`, which reports it rather than returning `[]`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest carta/mcp/tests/ -v`
Expected: PASS, all tests.

- [ ] **Step 8: Verify against the live backend**

Run:
```bash
python3 -c "
from pathlib import Path
from carta.config import load_config, find_config
from carta.mcp import server
cfg = load_config(find_config(Path.cwd()))
print(server._run_search_collection('axle', cfg, 'doc-audit-cc_notes', 3))
"
```
Expected: a list (possibly empty if the collection is empty) and **no** 400. Before this task it raised and was swallowed into `[]`.

- [ ] **Step 9: Commit**

```bash
git add carta/mcp/server.py carta/mcp/tests/fakes.py carta/mcp/tests/test_server.py
git commit -m "fix(mcp): pass using= on named-vector collections; stop swallowing query failures

carta_search returned [] on every hybrid collection: query_points() omitted
using=, Qdrant 400d, and the error was caught by the missing-collection
handler. Adds a contract-faithful Qdrant double so the permissive-mock gap
that let this ship is closed too."
```

---

### Task 2: Client-side dense+sparse fusion, behaviour-preserving at k=2

**Files:**
- Modify: `carta/embed/pipeline.py:1800-1830` (`_hybrid_query_collection`)
- Test: `carta/embed/tests/test_hybrid_query.py`

**Interfaces:**
- Produces: `_lane_queries(client, coll_name, query, dense_vec, prefetch_limit, bm25_model, query_filter) -> tuple[list, list]` returning `(dense_points, sparse_points)`; `_fuse_lanes(dense_points, sparse_points, top_n, k) -> list[dict]` where each dict is `{"point": <qdrant point>, "score": float, "ranks": {"dense": int | None, "sparse": int | None}}`.
- Consumed by: Task 3 (writeback), Task 5 (trace), Task 7 (gate).

- [ ] **Step 1: Write the failing parity test**

```python
# carta/embed/tests/test_hybrid_query.py  (append)
def test_fuse_lanes_matches_qdrant_rrf_at_k2():
    """Qdrant's server-side RRF is sum of 1/(k+rank) with k=2, rank 0-based."""
    dense = [_pt("a"), _pt("b"), _pt("c")]
    sparse = [_pt("b"), _pt("a"), _pt("d")]

    fused = pipeline._fuse_lanes(dense, sparse, top_n=4, k=2)

    # a: 1/2 + 1/3 = 0.8333 | b: 1/3 + 1/2 = 0.8333 | c: 1/4 | d: 1/4
    assert [f["point"].id for f in fused[:2]] == ["a", "b"]
    assert fused[0]["score"] == pytest.approx(1/2 + 1/3)
    assert fused[0]["ranks"] == {"dense": 0, "sparse": 1}


def test_fuse_lanes_admits_single_lane_hits():
    """A hit present in only one lane must still be admitted (dropping it is a
    silent recall bug), with the missing lane recorded as None."""
    fused = pipeline._fuse_lanes([_pt("a")], [], top_n=5, k=2)
    assert fused[0]["ranks"] == {"dense": 0, "sparse": None}
    assert fused[0]["score"] == pytest.approx(1/2)
```

Add the helper at the top of the module:

```python
def _pt(pid, score=1.0):
    p = MagicMock()
    p.id = pid
    p.score = score
    p.payload = {"file_path": f"docs/{pid}.md", "text": pid}
    return p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/embed/tests/test_hybrid_query.py -k fuse_lanes -v`
Expected: FAIL with `AttributeError: module 'carta.embed.pipeline' has no attribute '_fuse_lanes'`.

- [ ] **Step 3: Implement the lane queries and the fusion**

```python
# carta/embed/pipeline.py — replace _hybrid_query_collection's body

# Qdrant's server-side RRF uses k=2. Client-side fusion starts here to preserve
# ordering exactly; see docs/superpowers/specs/2026-08-09-retrieval-path-repair-
# and-tracing-design.md Component 2.
QDRANT_RRF_K = 2


def _lane_queries(client, coll_name, query, dense_vec, prefetch_limit,
                  bm25_model, query_filter=None):
    """Query the dense and sparse lanes separately and return both point lists."""
    sv = embed_sparse_query(query, model_name=bm25_model)
    dense_resp = client.query_points(
        collection_name=coll_name, query=dense_vec, using=DENSE_VECTOR_NAME,
        limit=prefetch_limit, query_filter=query_filter, with_payload=True,
    )
    sparse_resp = client.query_points(
        collection_name=coll_name,
        query=qmodels.SparseVector(indices=sv.indices, values=sv.values),
        using=SPARSE_VECTOR_NAME, limit=prefetch_limit,
        query_filter=query_filter, with_payload=True,
    )
    return dense_resp.points, sparse_resp.points


def _fuse_lanes(dense_points, sparse_points, top_n: int,
                k: int = QDRANT_RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion over two lanes, returning ranks alongside scores.

    Mirrors Qdrant's server-side RRF at k=2. A point present in only one lane is
    admitted with the other lane recorded as None — dropping it would be a silent
    recall bug.
    """
    acc: dict = {}
    for lane, points in (("dense", dense_points), ("sparse", sparse_points)):
        for rank, p in enumerate(points):
            entry = acc.setdefault(
                p.id, {"point": p, "score": 0.0,
                       "ranks": {"dense": None, "sparse": None}})
            entry["score"] += 1.0 / (k + rank)
            entry["ranks"][lane] = rank
    fused = sorted(acc.values(), key=lambda e: (-e["score"], str(e["point"].id)))
    return fused[:top_n]


def _hybrid_query_collection(client, coll_name, query, dense_vec, top_n,
                             prefetch_limit, bm25_model, query_filter=None,
                             rrf_k: int = QDRANT_RRF_K):
    """Hybrid BM25+dense query, fused client-side so per-lane ranks are available."""
    dense_points, sparse_points = _lane_queries(
        client, coll_name, query, dense_vec, prefetch_limit, bm25_model, query_filter,
    )
    return _fuse_lanes(dense_points, sparse_points, top_n, rrf_k)
```

Note: the return type changed from a Qdrant response object to a list of dicts. Update the caller at `pipeline.py:2431-2465` — it iterates `response.points`; it must now iterate the fused list and read `entry["point"].payload`.

- [ ] **Step 4: Update the caller**

```python
                    fused = _hybrid_query_collection(
                        client, coll_name, query, query_vec, fetch_limit,
                        prefetch_limit=hybrid_cfg.get("prefetch_limit", 40),
                        bm25_model=hybrid_cfg.get("bm25_model", "Qdrant/bm25"),
                    )
                    for entry in fused:
                        r = entry["point"]
                        payload = r.payload or {}
                        coll_results.append({
                            "score": entry["score"],
                            "lane_ranks": entry["ranks"],
                            "source": payload.get("file_path", payload.get("slug", "")),
                            "excerpt": payload.get("text", ""),
                            "type": "text",
                            "doc_type": payload.get("doc_type", ""),
                            "page": payload.get("page") or payload.get("page_num"),
                            "section_heading": payload.get("section_heading", ""),
                            "text_source": _text_source(payload),
                        })
```

The non-hybrid branches keep `response.points` and set `"lane_ranks": None`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/ -q`
Expected: PASS. `test_hybrid_query_uses_prefetch_and_rrf` will fail — it asserts on `prefetch`, which no longer exists. Rewrite it to assert two `query_points` calls with `using="dense"` and `using="bm25"` respectively.

- [ ] **Step 6: Verify parity against live Qdrant**

```bash
cd /Users/ian/School/Elementrailer/ET-embed && PYTHONPATH=/Users/ian/dev/doc-audit-cc python3 - <<'PY'
from pathlib import Path
from carta.config import load_config, find_config
from carta.embed.pipeline import run_search
cfg = load_config(find_config(Path.cwd()))
for q in ["trailer axle load rating", "brake controller wiring", "suspension mount",
          "torsion axle specifications", "wire gauge for lights"]:
    print([h["source"] for h in run_search(q, cfg)], "|", q)
PY
```
Expected: identical `source` ordering to the pre-change run. Record both outputs and diff them.

**Interpreting a difference.** Qdrant's tie-break among equal RRF scores is unspecified, while `_fuse_lanes` breaks ties deterministically by `str(point.id)`. So a difference is only a real regression if the *scores* differ or a result appears/disappears. Before concluding the refactor is wrong, print the fused scores alongside the sources:

- Same multiset of results, same scores, order differs **only within equal-score groups** → benign tie-break difference. Record it in the report and proceed.
- Different scores, or a result present in one run and absent in the other → **stop, the refactor is wrong.**

- [ ] **Step 7: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_hybrid_query.py
git commit -m "refactor(search): fuse dense+sparse client-side at k=2 (behaviour-preserving)

Qdrant fuses server-side and returns only the fused score, so per-lane ranks
never come back. Moves the intra-collection fusion into Python at k=2 to
match Qdrant exactly, verified by identical result ordering on live queries.
Ordering semantics unchanged; k becomes configurable in a later commit."
```

---

### Task 3: The hit carries the score that ranked it

**Files:**
- Modify: `carta/embed/pipeline.py:2190-2233` (`_rrf_merge_collections`)
- Test: `carta/embed/tests/test_visual_search_merge.py`

**Interfaces:**
- Produces: each returned hit dict gains `fused_score: float` and `fused_rank: int`. `score` and `lane_ranks` from Task 2 are left intact.

- [ ] **Step 1: Write the failing test**

```python
def test_merge_writes_fused_score_and_rank_onto_hits():
    """The cross-collection fusion decides ordering, so its score must be on the
    hit. Before this, hits carried the intra-collection score, which did not
    determine their rank."""
    a = {"source": "a.md", "score": 0.9}
    b = {"source": "b.md", "score": 0.1}
    out = pipeline._rrf_merge_collections([[a], [b]], top_n=2, k=60)

    assert out[0]["fused_rank"] == 0
    assert out[1]["fused_rank"] == 1
    assert out[0]["fused_score"] == pytest.approx(1.0 / 61)
    # intra-collection score preserved, not clobbered
    assert out[0]["score"] == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/embed/tests/test_visual_search_merge.py -k fused_score -v`
Expected: FAIL with `KeyError: 'fused_rank'`.

- [ ] **Step 3: Implement**

In `_rrf_merge_collections`, replace the final two lines:

```python
    ordered = []
    for fused_rank, (rrf, _coll_index, _rank, hit) in enumerate(scored):
        # Record the score that actually determined ordering. `score` keeps the
        # intra-collection value; consumers that need ranking magnitude read
        # fused_score. Gate and trace must agree on which number is which.
        hit["fused_score"] = rrf
        hit["fused_rank"] = fused_rank
        ordered.append(hit)
    return _apply_visual_cap(ordered, top_n, visual_max_ratio)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add carta/embed/pipeline.py carta/embed/tests/test_visual_search_merge.py
git commit -m "fix(search): record the cross-collection fused score on each hit

_rrf_merge_collections reordered hits without writing its score back, so
hits[0]['score'] held the intra-collection k=2 value — a number that did not
determine the ranking. Adds fused_score/fused_rank; leaves score intact."
```

---

### Task 4: `rrf_k` becomes configuration, default unchanged

**Files:**
- Modify: `carta/config.py` (`search.hybrid` defaults), `carta/embed/pipeline.py` (thread it through)
- Test: `carta/embed/tests/test_hybrid_query.py`

**Interfaces:**
- Produces: config key `search.hybrid.rrf_k`, default `2`.

- [ ] **Step 1: Write the failing test**

```python
def test_rrf_k_is_configurable_and_defaults_to_2():
    from carta.config import DEFAULTS
    assert DEFAULTS["search"]["hybrid"]["rrf_k"] == 2

    dense, sparse = [_pt("a"), _pt("b")], [_pt("b"), _pt("a")]
    at2 = pipeline._fuse_lanes(dense, sparse, top_n=2, k=2)
    at60 = pipeline._fuse_lanes(dense, sparse, top_n=2, k=60)
    # k flattens the curve: score spread shrinks as k grows
    assert (at2[0]["score"] - at2[1]["score"]) > (at60[0]["score"] - at60[1]["score"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/embed/tests/test_hybrid_query.py -k rrf_k -v`
Expected: FAIL with `KeyError: 'rrf_k'`.

- [ ] **Step 3: Add the config key**

In `carta/config.py`, inside `DEFAULTS["search"]["hybrid"]`, after `prefetch_limit`:

```python
            # Qdrant's server-side RRF used k=2; kept as the default so client-side
            # fusion is behaviour-identical. Flath's course and most literature use
            # 60. Changing it shifts ordering — do it against an eval, not by feel.
            "rrf_k": 2,
```

- [ ] **Step 4: Thread it through the caller**

At the `_hybrid_query_collection` call site in `run_search`, add:

```python
                        rrf_k=hybrid_cfg.get("rrf_k", 2),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest carta/embed/tests/ carta/tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add carta/config.py carta/embed/pipeline.py carta/embed/tests/test_hybrid_query.py
git commit -m "feat(search): make RRF k configurable, default 2 (unchanged behaviour)"
```

---

### Task 5: The trace primitive and the hook log

**Files:**
- Create: `carta/search/trace.py`, `carta/search/tests/test_trace.py`
- Modify: `carta/hook/hook.py`

**Interfaces:**
- Produces: `build_trace_record(*, query, collections, hits, zone, judge, latency_ms, score_kind, rrf_k) -> dict` and `append_trace(repo_root: Path, record: dict) -> None`.
- Consumed by: Task 6 (CLI), Task 7 (gate).

- [ ] **Step 1: Write the failing tests**

```python
# carta/search/tests/test_trace.py
import json
from pathlib import Path
from carta.search import trace


def test_record_captures_lane_ranks_and_zone():
    hits = [{"source": "docs/a.md", "score": 0.7, "fused_score": 0.016,
             "fused_rank": 0, "lane_ranks": {"dense": 0, "sparse": 4}}]
    rec = trace.build_trace_record(
        query="torsion axle spec", collections=["ET-embed_doc"], hits=hits,
        zone="judge", judge=True, latency_ms=412, score_kind="rrf", rrf_k=2,
    )
    assert rec["query"] == "torsion axle spec"
    assert rec["lanes"] == {"dense": 0, "sparse": 4}
    assert rec["zone"] == "judge" and rec["judge"] is True
    assert rec["score_kind"] == "rrf" and rec["rrf_k"] == 2
    assert "ts" in rec


def test_record_handles_zero_hits():
    rec = trace.build_trace_record(
        query="q", collections=[], hits=[], zone="silent", judge=None,
        latency_ms=8, score_kind="rrf", rrf_k=2)
    assert rec["lanes"] is None and rec["score"] is None


def test_append_writes_jsonl_and_creates_dir(tmp_path):
    rec = {"ts": "2026-08-09T00:00:00Z", "query": "q"}
    trace.append_trace(tmp_path, rec)
    files = list((tmp_path / ".carta" / "traces").glob("hook-*.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text().strip())["query"] == "q"


def test_append_never_raises(tmp_path, monkeypatch):
    """Instrumentation must never break the thing it instruments."""
    monkeypatch.setattr(trace, "_trace_path",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    trace.append_trace(tmp_path, {"query": "q"})   # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/search/tests/test_trace.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.search.trace'`.

- [ ] **Step 3: Implement the module**

```python
# carta/search/trace.py
"""Retrieval tracing: what each stage did with each result.

A retrieval miss can happen at five stages (never retrieved, one lane only,
demoted by fusion, collapsed by dedup, dropped by the visual cap) and they are
indistinguishable from the outside. This records which one.

Two consumers: the recall hook appends JSONL for gate calibration; `carta
search --trace` prints per-stage ranks for error analysis.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trace_path(repo_root: Path, when: Optional[str] = None) -> Path:
    stamp = (when or _utc_now_iso())[:7]          # YYYY-MM
    return repo_root / ".carta" / "traces" / f"hook-{stamp}.jsonl"


def build_trace_record(*, query: str, collections: list, hits: list, zone: str,
                       judge, latency_ms: int, score_kind: str,
                       rrf_k: Optional[int]) -> dict:
    """Build one trace record from a completed search.

    `query` is the DERIVED query (post `_extract_query`), never the raw prompt.
    `zone` is one of "silent" | "judge" | "inject".
    """
    top = hits[0] if hits else None
    return {
        "ts": _utc_now_iso(),
        "query": query,
        "collections": list(collections),
        "n_hits": len(hits),
        "top_source": top.get("source") if top else None,
        "lanes": top.get("lane_ranks") if top else None,
        "score": top.get("score") if top else None,
        "fused_score": top.get("fused_score") if top else None,
        "score_kind": score_kind,
        "rrf_k": rrf_k,
        "zone": zone,
        "judge": judge,
        "latency_ms": latency_ms,
    }


def append_trace(repo_root: Path, record: dict) -> None:
    """Append one JSONL record. Never raises — tracing must not break search."""
    try:
        # Derive the monthly file from the RECORD's own timestamp, not from a fresh
        # clock reading. Calling _trace_path(repo_root) bare decouples the two, so a
        # record written across a month boundary — or by any caller that buffers
        # before flushing — lands in a file whose name disagrees with its contents.
        # .get() keeps this total: _trace_path falls back to now when when is None.
        path = _trace_path(repo_root, when=record.get("ts"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
```

**Ruling (2026-08-11):** an earlier draft called `_trace_path(repo_root)` with no `when=`. The Task 5 review flagged it Important; the controller reproduced it (a record stamped `2026-07-31T23:59:59Z` landed in `hook-2026-08.jsonl`) and the human partner ruled the plan wrong. The snippet above is governing.

Task 5's tests must also cover two cases the original Step 1 tests missed:
- A record whose `ts` is in a **different month from the current one**, asserting the filename matches that `ts`. This must fail against the bare-call implementation.
- A **non-empty** hits list whose top dict omits `lane_ranks` (and `fused_score`) — the actual shape produced by both visual branches and the MCP text path — asserting the record is produced with `None` rather than raising.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/search/tests/test_trace.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add carta/search/trace.py carta/search/tests/test_trace.py
git commit -m "feat(search): add retrieval trace primitive (record + JSONL append)"
```

---

### Task 6: `carta search --trace <substring>`

**Files:**
- Modify: `carta/cli.py` (search subparser and handler), `carta/search/trace.py` (add `format_trace`)
- Test: `carta/search/tests/test_trace.py`

**Interfaces:**
- Produces: `format_trace(hits, needle: str, query: str, collections: list) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_format_trace_reports_stages_for_matching_doc():
    hits = [{"source": "docs/CAN/TOPOLOGY.md", "lane_ranks": {"dense": 7, "sparse": 3},
             "fused_score": 0.0161, "fused_rank": 1, "score": 0.7}]
    out = trace.format_trace(hits, "TOPOLOGY", "CAN termination", ["ET-embed_doc"])
    assert "TOPOLOGY.md" in out
    assert "dense rank" in out and "7" in out
    assert "bm25 rank" in out and "3" in out
    assert "FINAL" in out


def test_format_trace_says_not_retrieved_when_absent():
    out = trace.format_trace([], "US-11965795", "kingpin", ["ET-embed_doc"])
    assert "not retrieved" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/search/tests/test_trace.py -k format_trace -v`
Expected: FAIL with `AttributeError: module 'carta.search.trace' has no attribute 'format_trace'`.

- [ ] **Step 3: Implement `format_trace`**

```python
def format_trace(hits: list, needle: str, query: str, collections: list) -> str:
    """Human-readable per-stage report for documents matching `needle`."""
    lines = [
        f"derived query : {query}",
        f"collections   : {', '.join(collections) or '(none)'}",
        "",
    ]
    matches = [h for h in hits if needle.lower() in str(h.get("source", "")).lower()]
    if not matches:
        lines.append(f"{needle}")
        lines.append("  bm25 rank    : —  not in lane")
        lines.append("  dense rank   : —  not in lane")
        lines.append("  FINAL        : not retrieved")
        lines.append("")
        lines.append("  → never entered retrieval: check ingestion, not ranking.")
        return "\n".join(lines)

    for h in matches:
        ranks = h.get("lane_ranks") or {}
        dense = ranks.get("dense")
        sparse = ranks.get("sparse")
        lines.append(str(h.get("source")))
        lines.append(f"  bm25 rank    : {sparse if sparse is not None else '— not in lane'}")
        lines.append(f"  dense rank   : {dense if dense is not None else '— not in lane'}")
        lines.append(f"  post-RRF     : {h.get('fused_rank')}   "
                     f"(fused {h.get('fused_score')})")
        lines.append(f"  FINAL        : {hits.index(h)}  ✓ shown")
    return "\n".join(lines)
```

- [ ] **Step 4: Wire the CLI flag**

In `carta/cli.py`, on the `search` subparser:

```python
    search_parser.add_argument(
        "--trace", metavar="SUBSTRING",
        help="Print per-stage retrieval ranks for documents whose path matches "
             "SUBSTRING. Diagnoses which stage lost a result.",
    )
```

In the search handler, after `hits = run_search(...)`:

```python
    if getattr(args, "trace", None):
        from carta.search.trace import format_trace
        from carta.search.scoped import get_search_collections
        print(format_trace(hits, args.trace, query,
                           get_search_collections(cfg, "repo")))
        print()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest carta/search/tests/ carta/tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 6: Verify by hand against the live corpus**

```bash
cd /Users/ian/School/Elementrailer/ET-embed && \
  PYTHONPATH=/Users/ian/dev/doc-audit-cc python3 -m carta search "CAN termination" --trace TOPOLOGY
```
Expected: a stage report with real rank numbers.

- [ ] **Step 7: Commit**

```bash
git add carta/cli.py carta/search/trace.py carta/search/tests/test_trace.py
git commit -m "feat(cli): carta search --trace <substring> reports per-stage ranks"
```

---

### Task 7: Rank-and-agreement gate in the recall hook

**Files:**
- Modify: `carta/hook/hook.py:100-125`, `carta/config.py` (`proactive_recall` defaults)
- Test: `carta/hook/tests/test_hook.py`

**Interfaces:**
- Produces: `_gate_zone(hits, agree_rank: int) -> str` returning `"inject" | "judge" | "silent"`.
- Config: `proactive_recall.agree_rank`, default `3`. `high_threshold` / `low_threshold` retained for non-hybrid (cosine) searches.

- [ ] **Step 1: Write the failing tests**

```python
def test_gate_injects_when_both_lanes_agree():
    hits = [{"lane_ranks": {"dense": 0, "sparse": 1}}]
    assert hook._gate_zone(hits, agree_rank=3) == "inject"


def test_gate_judges_when_only_one_lane_is_confident():
    hits = [{"lane_ranks": {"dense": 0, "sparse": 40}}]
    assert hook._gate_zone(hits, agree_rank=3) == "judge"


def test_gate_judges_when_hit_is_in_one_lane_only():
    """The exact case the old gate dropped: rank 0 in one lane scored ~0.5,
    below low_threshold 0.60, and never reached the judge."""
    hits = [{"lane_ranks": {"dense": 0, "sparse": None}}]
    assert hook._gate_zone(hits, agree_rank=3) == "judge"


def test_gate_silent_when_neither_lane_is_confident():
    hits = [{"lane_ranks": {"dense": 9, "sparse": 12}}]
    assert hook._gate_zone(hits, agree_rank=3) == "silent"


def test_gate_silent_on_no_hits():
    assert hook._gate_zone([], agree_rank=3) == "silent"


def test_gate_falls_back_to_score_when_lane_ranks_absent():
    """Non-hybrid collections return cosine scores and no lane ranks; the old
    thresholds remain correct there."""
    assert hook._gate_zone([{"score": 0.9}], agree_rank=3,
                           low=0.6, high=0.85) == "inject"
    assert hook._gate_zone([{"score": 0.5}], agree_rank=3,
                           low=0.6, high=0.85) == "silent"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest carta/hook/tests/test_hook.py -k gate -v`
Expected: FAIL with `AttributeError: module 'carta.hook.hook' has no attribute '_gate_zone'`.

- [ ] **Step 3: Implement the gate**

```python
def _gate_zone(hits, agree_rank: int = 3, low: float = 0.60,
               high: float = 0.85) -> str:
    """Decide inject / judge / silent from retrieval STRUCTURE, not magnitude.

    Hybrid search returns RRF scores whose scale depends on k and lane count, so
    an absolute threshold on them is meaningless (see the 2026-08-09 spec). Rank
    is scale-independent: "top 3 in both lanes" means the same thing at any k.

    Falls back to score thresholds when lane ranks are unavailable, which is the
    non-hybrid path where the cosine calibration is still valid.
    """
    if not hits:
        return "silent"

    ranks = hits[0].get("lane_ranks")
    if not ranks:
        score = hits[0].get("score")
        if score is None or score < low:
            return "silent"
        return "inject" if score > high else "judge"

    confident = [r for r in (ranks.get("dense"), ranks.get("sparse"))
                 if r is not None and r < agree_rank]
    if len(confident) >= 2:
        return "inject"
    if len(confident) == 1:
        return "judge"
    return "silent"
```

- [ ] **Step 4: Replace the gate call site**

Replace `hook.py:108-121` (the noise gate, fast-path and gray-zone blocks) with:

```python
    # 8. Gate on retrieval structure
    pr_cfg = cfg.get("proactive_recall", {})
    zone = _gate_zone(
        hits,
        agree_rank=pr_cfg.get("agree_rank", 3),
        low=low_threshold, high=high_threshold,
    )
    judge_verdict = None
    if zone == "judge":
        judge_verdict = _judge_with_timeout(prompt, hits, cfg, judge_timeout_s)

    _emit_trace(query, hits, zone, judge_verdict, started_at, cfg)

    if zone == "inject" or judge_verdict:
        _inject(hits)
    sys.exit(0)
```

`hook.py` imports `Path` but **not** `time`. Add it to the stdlib import block (line 16-19), and capture `started_at = time.monotonic()` immediately before the `run_search` call at `hook.py:100`:

```python
import time
```

And add the trace emitter, which must never break the hook:

```python
def _emit_trace(query, hits, zone, judge_verdict, started_at, cfg):
    """Append one trace record. Swallows everything — the hook must fail open."""
    try:
        from carta.search.trace import build_trace_record, append_trace
        hybrid = cfg.get("search", {}).get("hybrid", {})
        rec = build_trace_record(
            query=query,
            collections=cfg.get("_trace_collections", []),
            hits=hits, zone=zone, judge=judge_verdict,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            score_kind="rrf" if hybrid.get("enabled", True) else "cosine",
            rrf_k=hybrid.get("rrf_k", 2) if hybrid.get("enabled", True) else None,
        )
        append_trace(Path(cfg["_repo_root"]), rec)
    except Exception:
        pass
```

- [ ] **Step 5: Add the config key**

In `carta/config.py`, inside `DEFAULTS["proactive_recall"]`:

```python
        # Top-N in BOTH lanes -> inject; in ONE lane -> judge; neither -> silent.
        # Placeholder value: calibrate against .carta/traces + issue #118 usage
        # labels rather than by feel.
        "agree_rank": 3,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest carta/hook/tests/ -q && python -m pytest carta/ -q`
Expected: PASS, full suite.

- [ ] **Step 7: Verify the hook end-to-end**

```bash
cd /Users/ian/School/Elementrailer/ET-embed && \
  echo '{"prompt":"torsion axle specifications"}' | \
  PYTHONPATH=/Users/ian/dev/doc-audit-cc python3 -m carta.hook.hook ; echo "exit=$?"
cat .carta/traces/hook-*.jsonl | tail -1
```
Expected: `exit=0`, and one JSONL line showing lane ranks and the chosen zone. Compare against the pre-change behaviour where four of five sample queries were silently dropped.

- [ ] **Step 8: Commit**

```bash
git add carta/hook/hook.py carta/config.py carta/hook/tests/test_hook.py
git commit -m "fix(hook): gate on lane rank agreement instead of absolute RRF score

The gate compared Qdrant RRF scores against cosine-calibrated thresholds
(0.60/0.85). Measured on ET-embed, 3 of 5 queries fell below low_threshold
and were dropped without reaching the judge, and the fast-path inject could
only fire at exactly 1.0. Gates on rank agreement, which is scale-independent.
Emits a trace record per invocation for calibration. Fail-open preserved."
```

---

## Self-Review

**Spec coverage.** Goal 1 → Task 1. Goal 2 → Tasks 2, 3. Goal 3 → Tasks 5, 6. Goal 4 → Tasks 4, 7. Goal 5 (legacy collections) → Task 1 Step 2 test and the non-hybrid branches in Task 2 Step 4. Spec Components 1/2a/2b/2c/3/4 → Tasks 1/2/4/3/5+6/7. All six spec test requirements are covered: named-vector (T1), legacy (T1), fusion parity (T2 Step 6), gate decision table (T7), trace resilience (T5), error propagation (T1).

**Placeholder scan.** No TBD/TODO. Every code step contains runnable code; every test step contains real assertions.

**Type consistency.** `_fuse_lanes` returns `{"point", "score", "ranks"}` (T2), which T2 Step 4 maps onto hit dicts as `score` and `lane_ranks`. `_rrf_merge_collections` adds `fused_score`/`fused_rank` (T3). `build_trace_record` reads `lane_ranks`, `score`, `fused_score`, `source` (T5) — all present. `_gate_zone` reads `lane_ranks` and `score` (T7) — both present. Names match across tasks.

**Known follow-up not in scope.** `_emit_trace` reads `cfg["_repo_root"]` and `cfg["_trace_collections"]`; if the hook's config does not already carry these, Task 7 Step 4 must thread them from the values `run_search` already computes. Flagged for the implementer rather than guessed at here.
