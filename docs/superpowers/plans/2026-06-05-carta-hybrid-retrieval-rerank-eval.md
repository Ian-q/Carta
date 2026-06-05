# Carta Hybrid Retrieval + Reranking + Eval Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise Carta's retrieval quality by adding a measurable eval harness, hybrid BM25+dense retrieval with RRF fusion, and a local cross-encoder reranker — all CPU-local, no API keys.

**Architecture:** Carta search is currently pure dense-vector cosine over Qdrant (`carta/embed/pipeline.py:run_search`). We add three layers, each independently togglable via `.carta/config.yaml`: (1) an offline eval harness that scores recall@k / MRR against a fixed query→expected-doc set so every later change is measured, not guessed; (2) a sparse BM25 vector alongside the existing dense vector in each Qdrant collection, fused at query time with Qdrant's native Reciprocal Rank Fusion (Query API); (3) a second-stage cross-encoder reranker over the fused candidate pool. Sparse encoding and reranking both come from `fastembed` (ONNX, CPU, fully local), so no new service and no API key.

**Tech Stack:** Python 3.10+, qdrant-client>=1.7 (Query API: `Prefetch` + `FusionQuery`, `SparseVectorParams` with IDF modifier), fastembed (`SparseTextEmbedding("Qdrant/bm25")`, `TextCrossEncoder("BAAI/bge-reranker-base")`), Ollama `nomic-embed-text` (unchanged dense path), pytest.

---

## Current State (verified against the repo, 2026-06-05)

- **Search:** `carta/embed/pipeline.py:932` `run_search()` → single dense `client.query_points(query=query_vec, ...)`. No sparse/BM25/fusion/rerank anywhere (`carta/mcp/server.py:57` `carta_search` calls into it). Embeddings are asymmetric Nomic: `"search_query: "` vs `"search_document: "` prefixes.
- **Collections:** created dense-only — `carta/embed/embed.py:74` `ensure_collection()` → `VectorParams(size=768, distance=COSINE)` (unnamed default vector). Visual collection (`ensure_visual_collection`, line 192) already uses a *named* multi-vector `"colpali"`, so named-vector collections are a pattern the codebase already supports.
- **Chunking:** `carta/embed/parse.py:143` `chunk_text()` — heading-aware, paragraph-greedy, `preserve_tables: true`. Chunk dicts carry `text`, `slug`, `file_path`, `doc_type`, `chunk_index`, `page`, `section_heading`.
- **Incremental indexing:** ALREADY DONE — `carta/embed/lifecycle.py` `compute_file_hash()` (SHA-256, LF-normalized) + `needs_rehash()` (mtime fast-path); per-file sidecars track `file_hash`/`file_mtime`/`generation`. **Do not re-implement.**
- **Config:** `carta/config.py` `DEFAULTS` + `load_config()` deep-merges `.carta/config.yaml`. `search:` block currently only has `top_n: 5`.
- **Tests:** pytest, `dev = ["pytest>=7.0"]`. Unit/integration heavy; **no retrieval-quality eval exists**.

**Out of scope (deferred, noted for later):** knowledge-graph layer over docs (build on `carta/search/graph.py`); making structural-audit IDs persistent stable `AUDIT-NNN` (the README claims persistence but `carta/audit/audit.py` generates ephemeral per-run IDs — a separate workstream).

---

## File Structure

| File | Create/Modify | Responsibility |
|------|---------------|----------------|
| `carta/eval/__init__.py` | Create | Package marker |
| `carta/eval/harness.py` | Create | Load eval set, run queries through a search fn, compute recall@k / MRR / hit-rate |
| `carta/eval/datasets/example.yaml` | Create | Documented schema example for a project eval set |
| `carta/eval/tests/__init__.py` | Create | Package marker |
| `carta/eval/tests/test_harness.py` | Create | Unit tests for metric math + eval-set loading (no Qdrant needed) |
| `carta/cli.py` | Modify | Add `carta eval` subcommand |
| `carta/config.py` | Modify | Extend `DEFAULTS["search"]` with `hybrid` + `rerank` blocks |
| `carta/embed/sparse.py` | Create | Lazy-loaded fastembed BM25 sparse encoder (doc + query) |
| `carta/embed/embed.py` | Modify | Named-vector collection schema (`dense` + sparse `bm25`); write sparse vectors on upsert |
| `carta/embed/pipeline.py` | Modify | Hybrid prefetch+RRF query path in `run_search()`, gated by config + collection schema detection |
| `carta/search/rerank.py` | Create | Lazy-loaded fastembed cross-encoder reranker |
| `pyproject.toml` | Modify | Add `fastembed` under a `hybrid` extra |
| `carta/embed/tests/test_sparse.py` | Create | Sparse encoder shape/caching tests |
| `carta/search/tests/test_rerank.py` | Create | Reranker ordering tests |
| `carta/embed/tests/test_hybrid_query.py` | Create | Hybrid query-construction tests (mocked Qdrant client) |

---

## Task 1: Retrieval eval harness

Build the measurement first, so Tasks 2–3 are proven, not asserted.

**Files:**
- Create: `carta/eval/__init__.py`
- Create: `carta/eval/harness.py`
- Create: `carta/eval/datasets/example.yaml`
- Create: `carta/eval/tests/__init__.py`
- Test: `carta/eval/tests/test_harness.py`
- Modify: `carta/cli.py`

- [ ] **Step 1: Write the failing test**

Create `carta/eval/tests/__init__.py` (empty) and `carta/eval/tests/test_harness.py`:

```python
from carta.eval.harness import load_eval_set, compute_metrics, EvalQuery


def test_load_eval_set(tmp_path):
    p = tmp_path / "set.yaml"
    p.write_text(
        "queries:\n"
        "  - q: what baud rate is the serial bridge\n"
        "    expect: [serial-bridge, CLAUDE.md]\n"
        "  - q: load cell counts per pound\n"
        "    expect: [bench-measurements]\n"
    )
    qs = load_eval_set(p)
    assert len(qs) == 2
    assert qs[0] == EvalQuery(q="what baud rate is the serial bridge",
                              expect=["serial-bridge", "CLAUDE.md"])


def test_compute_metrics_hit_and_miss():
    # Two queries; query A's expected substring appears at rank 2, query B misses entirely.
    eval_queries = [
        EvalQuery(q="A", expect=["alpha"]),
        EvalQuery(q="B", expect=["zeta"]),
    ]
    # results_per_query[i] = ordered list of file_path strings returned for query i
    results_per_query = [
        ["docs/other.md", "docs/alpha-spec.md", "docs/x.md"],  # hit at rank 2
        ["docs/p.md", "docs/q.md"],                            # no hit
    ]
    m = compute_metrics(eval_queries, results_per_query, k=3)
    assert m["n_queries"] == 2
    assert m["recall_at_k"] == 0.5          # 1 of 2 queries had an expected hit in top-3
    assert m["mrr"] == 0.25                 # (1/2 + 0) / 2
    assert m["per_query"][0]["first_hit_rank"] == 2
    assert m["per_query"][1]["first_hit_rank"] is None


def test_compute_metrics_respects_k_cutoff():
    eval_queries = [EvalQuery(q="A", expect=["alpha"])]
    results_per_query = [["x.md", "y.md", "alpha.md"]]  # hit at rank 3
    assert compute_metrics(eval_queries, results_per_query, k=2)["recall_at_k"] == 0.0
    assert compute_metrics(eval_queries, results_per_query, k=3)["recall_at_k"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/eval/tests/test_harness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.eval'`

- [ ] **Step 3: Write minimal implementation**

Create `carta/eval/__init__.py` (empty). Create `carta/eval/harness.py`:

```python
"""Offline retrieval-quality eval for Carta.

An eval set is a YAML file:

    queries:
      - q: "what baud rate is the serial bridge"
        expect: ["serial-bridge", "CLAUDE.md"]   # case-insensitive substrings of result file_path

A query "hits" if ANY expected substring is found in the file_path of a returned
result within the top-k. Metrics: recall@k (share of queries with >=1 hit) and
MRR (mean reciprocal rank of the first hit).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml


@dataclass(frozen=True)
class EvalQuery:
    q: str
    expect: list[str]


def load_eval_set(path: Path) -> list[EvalQuery]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    out: list[EvalQuery] = []
    for row in data.get("queries", []):
        out.append(EvalQuery(q=str(row["q"]), expect=[str(e) for e in row.get("expect", [])]))
    return out


def _first_hit_rank(expect: list[str], file_paths: list[str], k: int) -> Optional[int]:
    needles = [e.lower() for e in expect]
    for rank, fp in enumerate(file_paths[:k], start=1):
        hay = (fp or "").lower()
        if any(n in hay for n in needles):
            return rank
    return None


def compute_metrics(eval_queries: list[EvalQuery],
                    results_per_query: list[list[str]],
                    k: int) -> dict:
    per_query = []
    recall_hits = 0
    rr_sum = 0.0
    for eq, results in zip(eval_queries, results_per_query):
        rank = _first_hit_rank(eq.expect, results, k)
        if rank is not None:
            recall_hits += 1
            rr_sum += 1.0 / rank
        per_query.append({"q": eq.q, "first_hit_rank": rank})
    n = len(eval_queries)
    return {
        "n_queries": n,
        "k": k,
        "recall_at_k": (recall_hits / n) if n else 0.0,
        "mrr": (rr_sum / n) if n else 0.0,
        "per_query": per_query,
    }


def run_eval(eval_path: Path,
             search_fn: Callable[[str, int], list[dict]],
             k: int = 5) -> dict:
    """Run an eval set through `search_fn(query, k) -> [{file_path, score, ...}]`."""
    eval_queries = load_eval_set(eval_path)
    results_per_query: list[list[str]] = []
    for eq in eval_queries:
        hits = search_fn(eq.q, k) or []
        results_per_query.append([h.get("file_path", "") for h in hits])
    return compute_metrics(eval_queries, results_per_query, k)
```

Create `carta/eval/datasets/example.yaml`:

```yaml
# Carta retrieval eval set.
# A query "hits" if any `expect` substring appears in a returned result's file_path
# within top-k. Keep 15-40 queries that cover the documents you actually ask about.
queries:
  - q: "what baud rate does the serial bridge use"
    expect: ["CLAUDE.md", "serial-bridge"]
  - q: "load cell counts per pound sensitivity"
    expect: ["bench-measurements", "loadcell"]
  - q: "VCU torque versus speed mode bit on CAN 0x200"
    expect: ["main.c", "torque"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/eval/tests/test_harness.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Wire the `carta eval` CLI subcommand**

In `carta/cli.py`, locate where subparsers are registered (the `add_parser(...)` calls for `embed`/`search`/`audit`). Add alongside them:

```python
    eval_p = subparsers.add_parser("eval", help="Score retrieval quality against an eval set")
    eval_p.add_argument("eval_path", help="Path to eval-set YAML (see carta/eval/datasets/example.yaml)")
    eval_p.add_argument("-k", type=int, default=5, help="top-k cutoff (default 5)")
    eval_p.add_argument("--scope", default="repo", choices=["repo", "shared", "global"])
```

Then in the command dispatch block (where `args.command == "search":` etc. are handled), add:

```python
    if args.command == "eval":
        from pathlib import Path
        from carta.config import load_config, find_config
        from carta.eval.harness import run_eval
        from carta.embed.pipeline import run_search

        cfg = load_config(find_config())
        k = args.k

        def _search(query: str, top_k: int) -> list:
            return run_search(query, cfg, top_n=top_k, scope=args.scope)

        metrics = run_eval(Path(args.eval_path), _search, k=k)
        print(f"queries={metrics['n_queries']}  recall@{k}={metrics['recall_at_k']:.3f}  MRR={metrics['mrr']:.3f}")
        for row in metrics["per_query"]:
            mark = row["first_hit_rank"] if row["first_hit_rank"] is not None else "MISS"
            print(f"  [{mark}] {row['q']}")
        return 0
```

> NOTE: match `run_search`'s real signature in `carta/embed/pipeline.py:932`. If its params differ (e.g. it takes a prebuilt client or different kwarg names), adapt the `_search` closure accordingly — the harness only needs a `(query, k) -> [{file_path}]` callable.

- [ ] **Step 6: Verify the CLI runs end-to-end against real embedded data**

Run: `cd /Users/ian/dev/doc-audit-cc && carta eval carta/eval/datasets/example.yaml -k 5`
Expected: a line like `queries=3  recall@5=0.xxx  MRR=0.xxx` plus per-query marks. (Requires Qdrant up and docs embedded; this is the dense-only baseline to beat.)

- [ ] **Step 7: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/eval pyproject.toml carta/cli.py
git commit -m "feat(eval): offline retrieval eval harness (recall@k, MRR) + carta eval CLI"
```

---

## Task 2: Hybrid BM25 + dense retrieval with RRF fusion

**Files:**
- Modify: `pyproject.toml`
- Modify: `carta/config.py`
- Create: `carta/embed/sparse.py`
- Test: `carta/embed/tests/test_sparse.py`
- Modify: `carta/embed/embed.py:74` (`ensure_collection`) and the upsert path (`upsert_chunks`, ~line 100-140)
- Modify: `carta/embed/pipeline.py:932` (`run_search`)
- Test: `carta/embed/tests/test_hybrid_query.py`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, under `[project.optional-dependencies]`, add:

```toml
hybrid = [
    "fastembed>=0.4",
]
```

Install: `cd /Users/ian/dev/doc-audit-cc && pip install -e ".[hybrid]"`

- [ ] **Step 2: Write the failing test for the sparse encoder**

Create `carta/embed/tests/test_sparse.py`:

```python
import pytest

from carta.embed.sparse import embed_sparse_document, embed_sparse_query, SparseVec


def test_sparse_document_returns_aligned_indices_and_values():
    sv = embed_sparse_document("the serial bridge runs at 921600 baud")
    assert isinstance(sv, SparseVec)
    assert len(sv.indices) == len(sv.values)
    assert len(sv.indices) > 0
    assert all(v >= 0 for v in sv.values)


def test_sparse_query_shape_matches():
    sv = embed_sparse_query("serial bridge baud rate")
    assert isinstance(sv, SparseVec)
    assert len(sv.indices) == len(sv.values)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_sparse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.embed.sparse'`

- [ ] **Step 4: Implement the sparse encoder**

Create `carta/embed/sparse.py`:

```python
"""Local BM25 sparse encoder via fastembed (ONNX, CPU, no API key).

The model is loaded lazily and cached process-wide. Document and query use the
same model; Qdrant applies the IDF modifier server-side (set on the collection),
so we only ship raw term frequencies here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

DEFAULT_BM25_MODEL = "Qdrant/bm25"


@dataclass(frozen=True)
class SparseVec:
    indices: list[int]
    values: list[float]


@lru_cache(maxsize=4)
def _model(model_name: str):
    from fastembed import SparseTextEmbedding
    return SparseTextEmbedding(model_name=model_name)


def _to_sparsevec(emb) -> SparseVec:
    # fastembed SparseEmbedding exposes .indices and .values as numpy arrays
    return SparseVec(indices=[int(i) for i in emb.indices],
                     values=[float(v) for v in emb.values])


def embed_sparse_document(text: str, model_name: str = DEFAULT_BM25_MODEL) -> SparseVec:
    return _to_sparsevec(next(iter(_model(model_name).embed([text]))))


def embed_sparse_query(text: str, model_name: str = DEFAULT_BM25_MODEL) -> SparseVec:
    return _to_sparsevec(next(iter(_model(model_name).query_embed([text]))))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_sparse.py -v`
Expected: PASS (first run downloads the small BM25 model to the fastembed cache, then PASS)

- [ ] **Step 6: Add config defaults**

In `carta/config.py`, find `DEFAULTS` and replace the `"search"` block with:

```python
    "search": {
        "top_n": 5,
        "hybrid": {
            "enabled": True,
            "bm25_model": "Qdrant/bm25",
            "prefetch_limit": 40,   # candidates pulled per branch before fusion
        },
        "rerank": {
            "enabled": False,       # turned on in Task 3
            "model": "BAAI/bge-reranker-base",
            "candidate_pool": 30,   # fused hits reranked before truncating to top_n
        },
    },
```

- [ ] **Step 7: Make collections named-vector + sparse-aware**

In `carta/embed/embed.py`, replace `ensure_collection` (line ~74) with a schema that has a named `dense` vector plus a `bm25` sparse vector, and add a schema-detection helper:

```python
from qdrant_client.models import (
    VectorParams, Distance, SparseVectorParams, Modifier,
)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "bm25"


def ensure_collection(client, coll_name):
    if not client.collection_exists(coll_name):
        client.create_collection(
            collection_name=coll_name,
            vectors_config={
                DENSE_VECTOR_NAME: VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF),
            },
        )


def collection_is_hybrid(client, coll_name) -> bool:
    """True if the collection has the named dense+sparse hybrid schema."""
    try:
        info = client.get_collection(coll_name)
        vectors = info.config.params.vectors
        has_named_dense = isinstance(vectors, dict) and DENSE_VECTOR_NAME in vectors
        sparse = getattr(info.config.params, "sparse_vectors", None)
        has_sparse = bool(sparse) and SPARSE_VECTOR_NAME in sparse
        return has_named_dense and has_sparse
    except Exception:
        return False
```

> MIGRATION: existing collections use an *unnamed* dense vector and have no sparse vector, so they are not hybrid. A one-time `carta embed --force` re-creates them under the new schema. `collection_is_hybrid()` lets the query path fall back to the legacy dense call for any not-yet-migrated collection (Step 10), so nothing breaks mid-migration.

- [ ] **Step 8: Write both vectors on upsert**

In `carta/embed/embed.py`, in `upsert_chunks` (~line 100-140) where each `PointStruct` is built, change the vector from the bare dense list to the named form and attach the sparse vector. Locate the existing point construction (it currently passes `vector=<dense_list>`) and replace with:

```python
from qdrant_client.models import PointStruct, SparseVector
from carta.embed.sparse import embed_sparse_document

# inside the per-chunk loop, where `dense_vec` is the Ollama embedding and
# `chunk["text"]` is the chunk body:
sv = embed_sparse_document(chunk["text"])
point = PointStruct(
    id=point_id,
    vector={
        DENSE_VECTOR_NAME: dense_vec,
        SPARSE_VECTOR_NAME: SparseVector(indices=sv.indices, values=sv.values),
    },
    payload=payload,
)
```

> Keep everything else (point_id generation, payload schema) identical.

- [ ] **Step 9: Write the failing hybrid-query test**

Create `carta/embed/tests/test_hybrid_query.py`:

```python
from unittest.mock import MagicMock

from carta.embed import pipeline


def test_hybrid_query_uses_prefetch_and_rrf(monkeypatch):
    captured = {}

    class FakeClient:
        def query_points(self, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.points = []
            return resp

    monkeypatch.setattr(pipeline, "collection_is_hybrid", lambda c, n: True)
    monkeypatch.setattr(pipeline, "get_embedding", lambda *a, **k: [0.0] * 768)
    monkeypatch.setattr(pipeline, "embed_sparse_query",
                        lambda *a, **k: pipeline._SparseVecShim([1, 2], [0.5, 0.5])
                        if hasattr(pipeline, "_SparseVecShim") else None)

    pipeline._hybrid_query_collection(
        FakeClient(), "ET-embed_doc", "serial bridge baud",
        dense_vec=[0.0] * 768, top_n=5, prefetch_limit=40, bm25_model="Qdrant/bm25",
    )

    assert "prefetch" in captured
    assert len(captured["prefetch"]) == 2          # dense branch + sparse branch
    assert captured["limit"] == 5
```

> This test pins the *shape* of the hybrid call (two prefetch branches + fusion + limit). Adjust the import path of `_hybrid_query_collection` to wherever you place it in Step 10.

- [ ] **Step 10: Implement the hybrid query path**

In `carta/embed/pipeline.py`, add imports at top:

```python
from carta.embed.embed import collection_is_hybrid, DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from carta.embed.sparse import embed_sparse_query
from qdrant_client import models as qmodels
```

Add a helper and route `run_search` through it. Inside the per-collection search loop in `run_search` (where it currently calls `client.query_points(query=query_vec, ...)`), branch:

```python
def _hybrid_query_collection(client, coll_name, query, dense_vec, top_n,
                             prefetch_limit, bm25_model):
    sv = embed_sparse_query(query, model_name=bm25_model)
    resp = client.query_points(
        collection_name=coll_name,
        prefetch=[
            qmodels.Prefetch(query=dense_vec, using=DENSE_VECTOR_NAME, limit=prefetch_limit),
            qmodels.Prefetch(
                query=qmodels.SparseVector(indices=sv.indices, values=sv.values),
                using=SPARSE_VECTOR_NAME, limit=prefetch_limit,
            ),
        ],
        query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
        limit=top_n,
        with_payload=True,
    )
    return resp


# in run_search, replacing the single dense call for each collection:
hybrid_cfg = cfg.get("search", {}).get("hybrid", {})
if hybrid_cfg.get("enabled", False) and collection_is_hybrid(client, coll_name):
    response = _hybrid_query_collection(
        client, coll_name, query, query_vec, top_n,
        prefetch_limit=hybrid_cfg.get("prefetch_limit", 40),
        bm25_model=hybrid_cfg.get("bm25_model", "Qdrant/bm25"),
    )
else:
    # legacy dense-only path (named OR unnamed vector), unchanged behavior
    response = client.query_points(
        collection_name=coll_name, query=query_vec, limit=top_n, with_payload=True,
    )
```

> If the collection was migrated to named vectors but hybrid is disabled, pass `using=DENSE_VECTOR_NAME` to the legacy call. Detect via `collection_is_hybrid`; when True but hybrid disabled, add `using=DENSE_VECTOR_NAME` to the dense-only `query_points`.

- [ ] **Step 11: Run tests**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/embed/tests/test_hybrid_query.py carta/embed/tests/test_sparse.py -v`
Expected: PASS

- [ ] **Step 12: Re-embed and measure the lift**

```bash
cd /Users/ian/dev/doc-audit-cc
carta embed --force                                  # migrate collections to hybrid schema
carta eval carta/eval/datasets/example.yaml -k 5     # compare recall@5 / MRR vs Task 1 baseline
```
Expected: recall@5 and/or MRR ≥ the dense-only baseline. Record both numbers in the commit message.

- [ ] **Step 13: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add pyproject.toml carta/config.py carta/embed/sparse.py carta/embed/embed.py carta/embed/pipeline.py carta/embed/tests/
git commit -m "feat(search): hybrid BM25+dense retrieval with RRF fusion (baseline recall@5 X.xx -> Y.yy)"
```

---

## Task 3: Local cross-encoder reranker

**Files:**
- Create: `carta/search/rerank.py`
- Test: `carta/search/tests/test_rerank.py`
- Modify: `carta/embed/pipeline.py` (`run_search`, after fusion/merge, before final truncation)

- [ ] **Step 1: Write the failing test**

Create `carta/search/tests/test_rerank.py`:

```python
from carta.search.rerank import rerank_hits


def test_rerank_reorders_by_cross_encoder_score(monkeypatch):
    import carta.search.rerank as r
    # Fake cross-encoder: score = +1 if "baud" in the chunk text, else 0.
    monkeypatch.setattr(r, "_scores",
                        lambda query, texts, model_name: [1.0 if "baud" in t else 0.0 for t in texts])

    hits = [
        {"text": "unrelated content", "score": 0.99, "file_path": "a.md"},
        {"text": "the bridge runs at 921600 baud", "score": 0.10, "file_path": "b.md"},
    ]
    out = rerank_hits("serial bridge baud", hits, model_name="x", top_n=2)
    assert out[0]["file_path"] == "b.md"            # promoted despite lower dense score
    assert out[0]["rerank_score"] == 1.0


def test_rerank_truncates_to_top_n(monkeypatch):
    import carta.search.rerank as r
    monkeypatch.setattr(r, "_scores", lambda q, texts, m: list(range(len(texts))))
    hits = [{"text": str(i), "file_path": f"{i}.md"} for i in range(5)]
    out = rerank_hits("q", hits, model_name="x", top_n=2)
    assert len(out) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_rerank.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'carta.search.rerank'`

- [ ] **Step 3: Implement the reranker**

Create `carta/search/rerank.py`:

```python
"""Local second-stage cross-encoder reranker via fastembed TextCrossEncoder.

Lazy-loaded, cached. Reorders fused candidates by query-chunk relevance and
truncates to top_n. No API key, CPU/ONNX.
"""
from __future__ import annotations

from functools import lru_cache

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


@lru_cache(maxsize=2)
def _model(model_name: str):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=model_name)


def _scores(query: str, texts: list[str], model_name: str) -> list[float]:
    return [float(s) for s in _model(model_name).rerank(query, texts)]


def rerank_hits(query: str, hits: list[dict], model_name: str, top_n: int) -> list[dict]:
    if not hits:
        return hits
    texts = [h.get("text", "") for h in hits]
    scores = _scores(query, texts, model_name)
    for h, s in zip(hits, scores):
        h["rerank_score"] = s
    hits.sort(key=lambda h: h["rerank_score"], reverse=True)
    return hits[:top_n]
```

Create `carta/search/tests/__init__.py` (empty) if it does not already exist.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest carta/search/tests/test_rerank.py -v`
Expected: PASS

- [ ] **Step 5: Wire reranking into `run_search`**

In `carta/embed/pipeline.py`, after results from all collections are merged and sorted but **before** the final top-n truncation/return, add:

```python
    rr_cfg = cfg.get("search", {}).get("rerank", {})
    if rr_cfg.get("enabled", False) and all_results:
        from carta.search.rerank import rerank_hits
        pool = all_results[: rr_cfg.get("candidate_pool", 30)]
        all_results = rerank_hits(query, pool, model_name=rr_cfg.get("model", "BAAI/bge-reranker-base"),
                                  top_n=top_n)
```

> `all_results` is the merged list of dicts each containing `text`, `score`, `file_path` (per `run_search`'s existing result shape). Ensure `text` is present in payload-derived results — it is (payload carries `text`, per `embed.py:124-140`).

- [ ] **Step 6: Enable rerank and measure**

Set `search.rerank.enabled: true` in `.carta/config.yaml`, then:
```bash
cd /Users/ian/dev/doc-audit-cc && carta eval carta/eval/datasets/example.yaml -k 5
```
Expected: MRR improves vs Task 2 (reranking mainly lifts ordering → MRR/precision@1). Record numbers.

- [ ] **Step 7: Run the full test suite**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m pytest -q`
Expected: all green (no regressions in existing 62 test files).

- [ ] **Step 8: Commit**

```bash
cd /Users/ian/dev/doc-audit-cc
git add carta/search/rerank.py carta/search/tests/ carta/embed/pipeline.py
git commit -m "feat(search): local cross-encoder reranker stage (MRR X.xx -> Y.yy)"
```

---

## Self-Review

**Spec coverage:**
- Hybrid BM25+dense + fusion → Task 2 ✓
- Local reranker (no API key) → Task 3 ✓ (fastembed `TextCrossEncoder`, CPU)
- Eval harness (recall@k/MRR, query→expected pairs) → Task 1 ✓
- Incremental indexing → intentionally OMITTED (already exists in `lifecycle.py`); noted in Current State ✓
- KG-over-docs layer + AUDIT-NNN persistence → explicitly deferred in "Out of scope" ✓
- Solo-maintainer leverage ordering (measure → biggest win → ordering polish) → task order ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"write tests for the above". Every code step ships real code; every test step ships an assertion. Two explicitly-flagged adaptation points (real `run_search` signature in T1S5/T3S5; sparse-vector mixed-schema fallback in T2S10) are integration seams against code this plan can't see line-for-line, not placeholders.

**Type consistency:** `SparseVec(indices, values)` defined in `sparse.py` (T2S4) and consumed in `embed.py` upsert (T2S8) and `pipeline._hybrid_query_collection` (T2S10). `EvalQuery(q, expect)` defined and used consistently (T1). `rerank_hits(query, hits, model_name, top_n)` signature matches between test (T3S1), impl (T3S3), and call site (T3S5). Collection vector names `DENSE_VECTOR_NAME`/`SPARSE_VECTOR_NAME` defined once in `embed.py` and imported everywhere. Config keys `search.hybrid.{enabled,bm25_model,prefetch_limit}` and `search.rerank.{enabled,model,candidate_pool}` are consistent across `config.py` defaults and all read sites.

**Known integration risks (validate during execution):** (1) qdrant-client API surface for `Prefetch`/`FusionQuery` requires a reasonably recent 1.7+; if the installed client is older, bump it. (2) fastembed `SparseEmbedding`/`TextCrossEncoder` attribute names (`.indices`/`.values`, `.rerank`) are stable in 0.4+ but verify against the installed version. (3) `run_search`'s real signature and result-dict keys must be confirmed before T1S5 and T3S5.
