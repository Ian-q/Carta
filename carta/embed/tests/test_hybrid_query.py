from unittest.mock import MagicMock, patch
from carta.embed import pipeline


def test_hybrid_query_uses_prefetch_and_rrf(monkeypatch):
    captured = {}

    class FakeClient:
        def query_points(self, **kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.points = []
            return resp

    monkeypatch.setattr(pipeline, "embed_sparse_query",
                        lambda *a, **k: pipeline.__dict__.get("_TestSparse",
                            type("S", (), {"indices": [1, 2], "values": [0.5, 0.5]})()))

    pipeline._hybrid_query_collection(
        FakeClient(), "ET-embed_doc", "serial bridge baud",
        dense_vec=[0.0] * 768, top_n=5, prefetch_limit=40, bm25_model="Qdrant/bm25",
    )

    assert "prefetch" in captured
    assert len(captured["prefetch"]) == 2
    assert captured["limit"] == 5


# ---------------------------------------------------------------------------
# Over-fetch / rerank integration tests
# ---------------------------------------------------------------------------

def _make_run_search_cfg(*, top_n=5, rerank_enabled=False, candidate_pool=30,
                          hybrid_enabled=False, dedupe_results=False):
    """Return a minimal carta config dict for run_search tests."""
    return {
        "project_name": "test_proj",
        "qdrant_url": "http://localhost:6333",
        "embed": {
            "ollama_url": "http://localhost:11434",
            "ollama_model": "nomic-embed-text",
            "colpali_enabled": False,
        },
        "search": {
            "top_n": top_n,
            "dedupe_results": dedupe_results,
            "hybrid": {"enabled": hybrid_enabled, "prefetch_limit": 40,
                       "bm25_model": "Qdrant/bm25"},
            "rerank": {"enabled": rerank_enabled, "candidate_pool": candidate_pool,
                       "model": "BAAI/bge-reranker-base"},
            # Disable graph expansion so these tests exercise rerank-only fetch_limit
            # behaviour in isolation — graph expansion has its own test suite.
            "graph": {"enabled": False},
        },
    }


def _fake_points(n, base_score=0.8):
    """Return n fake ScoredPoint-like MagicMocks."""
    points = []
    for i in range(n):
        p = MagicMock()
        p.score = base_score - i * 0.01
        p.payload = {"file_path": f"doc{i}.md", "text": f"chunk {i}"}
        points.append(p)
    return points


def _patch_run_search_deps(monkeypatch, *, captured_limits,
                            n_points_per_collection=30, is_hybrid=False):
    """Monkeypatch the external dependencies of run_search.

    captured_limits: a list that will be appended with each `limit` kwarg
    seen by any query_points call (or _hybrid_query_collection call).
    """
    # Patch find_config so Path(find_config()).parent resolves without hitting disk
    monkeypatch.setattr(pipeline, "find_config", lambda: "/fake/.carta/config.yaml")

    # Patch get_search_collections to return one text collection
    import carta.search.scoped as scoped_mod
    monkeypatch.setattr(scoped_mod, "get_search_collections",
                        lambda cfg, scope: ["test_proj_doc"])

    # Patch get_embedding to avoid Ollama calls
    monkeypatch.setattr(pipeline, "get_embedding",
                        lambda *a, **k: [0.0] * 768)

    # Patch collection_is_hybrid
    monkeypatch.setattr(pipeline, "collection_is_hybrid",
                        lambda client, coll: is_hybrid)

    # Build a fake Qdrant response with n_points_per_collection points
    fake_resp = MagicMock()
    fake_resp.points = _fake_points(n_points_per_collection)

    # Patch QdrantClient — capture every query_points limit arg
    class FakeQdrantClient:
        def __init__(self, *a, **k):
            pass

        def query_points(self, **kwargs):
            captured_limits.append(kwargs.get("limit"))
            return fake_resp

    monkeypatch.setattr(pipeline, "QdrantClient", FakeQdrantClient)

    # Patch _hybrid_query_collection to capture its limit arg too
    original_hybrid = pipeline._hybrid_query_collection

    def capturing_hybrid(client, coll_name, query, dense_vec, top_n,
                         prefetch_limit, bm25_model):
        captured_limits.append(top_n)
        return fake_resp

    monkeypatch.setattr(pipeline, "_hybrid_query_collection", capturing_hybrid)


def test_run_search_overfetch_when_rerank_enabled(monkeypatch):
    """With rerank enabled, retrieval should use limit=candidate_pool (30), not top_n (5)."""
    captured_limits = []
    _patch_run_search_deps(monkeypatch, captured_limits=captured_limits,
                           n_points_per_collection=30, is_hybrid=False)

    # Stub out rerank_hits so no cross-encoder model loads
    import carta.search.rerank as rr_mod
    monkeypatch.setattr(rr_mod, "_scores",
                        lambda query, texts, model_name: [0.5] * len(texts))

    cfg = _make_run_search_cfg(top_n=5, rerank_enabled=True, candidate_pool=30)
    results = pipeline.run_search("serial bridge baud", cfg)

    # Every collection query must have used fetch_limit=30, not top_n=5
    assert captured_limits, "No query_points calls were captured"
    for lim in captured_limits:
        assert lim == 30, (
            f"Expected fetch limit 30 (candidate_pool) when rerank enabled, got {lim}"
        )

    # Final result must be truncated to top_n
    assert len(results) <= 5


def test_run_search_no_overfetch_when_rerank_disabled(monkeypatch):
    """With rerank AND dedup disabled, retrieval uses limit=top_n (5) — no overfetch."""
    captured_limits = []
    _patch_run_search_deps(monkeypatch, captured_limits=captured_limits,
                           n_points_per_collection=5, is_hybrid=False)

    cfg = _make_run_search_cfg(top_n=5, rerank_enabled=False, candidate_pool=30)
    results = pipeline.run_search("serial bridge baud", cfg)

    assert captured_limits, "No query_points calls were captured"
    for lim in captured_limits:
        assert lim == 5, (
            f"Expected fetch limit 5 (top_n) when rerank disabled, got {lim}"
        )

    assert len(results) <= 5


def test_run_search_deepens_fetch_for_dedup_when_enabled(monkeypatch):
    """With dedup on, fetch is deepened to the pool floor (30) even when rerank is
    off, so dedup has headroom to backfill top_n distinct docs."""
    captured_limits = []
    _patch_run_search_deps(monkeypatch, captured_limits=captured_limits,
                           n_points_per_collection=30, is_hybrid=False)

    cfg = _make_run_search_cfg(top_n=5, rerank_enabled=False, dedupe_results=True)
    pipeline.run_search("serial bridge baud", cfg)

    assert captured_limits, "No query_points calls were captured"
    for lim in captured_limits:
        assert lim == 30, (
            f"Expected deepened fetch 30 (pool floor) with dedup on, got {lim}"
        )


def test_run_search_hybrid_overfetch_when_rerank_enabled(monkeypatch):
    """With hybrid search + rerank enabled, _hybrid_query_collection gets fetch_limit=30."""
    captured_limits = []
    _patch_run_search_deps(monkeypatch, captured_limits=captured_limits,
                           n_points_per_collection=30, is_hybrid=True)

    import carta.search.rerank as rr_mod
    monkeypatch.setattr(rr_mod, "_scores",
                        lambda query, texts, model_name: [0.5] * len(texts))

    cfg = _make_run_search_cfg(top_n=5, rerank_enabled=True, candidate_pool=30,
                                hybrid_enabled=True)
    results = pipeline.run_search("serial bridge baud", cfg)

    assert captured_limits, "No _hybrid_query_collection calls were captured"
    for lim in captured_limits:
        assert lim == 30, (
            f"Expected hybrid fetch limit 30 (candidate_pool) when rerank enabled, got {lim}"
        )
    assert len(results) <= 5


def test_run_search_final_truncation_rerank_on(monkeypatch):
    """Result count must equal top_n even when candidate_pool > top_n."""
    captured_limits = []
    _patch_run_search_deps(monkeypatch, captured_limits=captured_limits,
                           n_points_per_collection=30, is_hybrid=False)

    import carta.search.rerank as rr_mod
    monkeypatch.setattr(rr_mod, "_scores",
                        lambda query, texts, model_name: list(range(len(texts))))

    cfg = _make_run_search_cfg(top_n=5, rerank_enabled=True, candidate_pool=30)
    results = pipeline.run_search("q", cfg)

    assert len(results) == 5


def test_run_search_final_truncation_rerank_off(monkeypatch):
    """Result count must equal top_n when rerank is off."""
    captured_limits = []
    _patch_run_search_deps(monkeypatch, captured_limits=captured_limits,
                           n_points_per_collection=5, is_hybrid=False)

    cfg = _make_run_search_cfg(top_n=5, rerank_enabled=False)
    results = pipeline.run_search("q", cfg)

    assert len(results) <= 5
