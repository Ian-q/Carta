"""run_search(..., hypothetical=) — HyDE on the CLI/hybrid path.

Only the dense lane may see the hypothetical: BM25 must keep the raw query (a
hypothetical's invented identifiers would pollute lexical matching), and with no
hypothetical nothing may change. Spec: docs/superpowers/specs/2026-09-21-hyde-hypothetical-query-design.md
"""
import math

import pytest

import carta.embed.pipeline as pipeline
import carta.search.hyde as hyde

Q_VEC = [1.0, 0.0]
H_VEC = [0.0, 1.0]
BLEND = [1 / math.sqrt(2), 1 / math.sqrt(2)]


@pytest.fixture
def rig(monkeypatch):
    calls = {"lane": [], "hyp": []}
    monkeypatch.setattr(pipeline, "find_config", lambda: "/fake/.carta/config.yaml")
    import carta.search.scoped as scoped_mod
    monkeypatch.setattr(scoped_mod, "get_search_collections", lambda cfg, scope: ["p_doc"])
    monkeypatch.setattr(pipeline, "_embed_query_or_raise", lambda q, cfg, colls, timeout=None: list(Q_VEC))
    monkeypatch.setattr(pipeline, "collection_is_hybrid", lambda c, n: True)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(pipeline, "QdrantClient", FakeClient)

    def fake_hybrid(client, coll_name, query, dense_vec, top_n, **kw):
        calls["lane"].append((query, list(dense_vec)))
        return []

    monkeypatch.setattr(pipeline, "_hybrid_query_collection", fake_hybrid)

    def fake_embed_hyp(text, ollama_url, model, timeout=None):
        calls["hyp"].append((text, timeout))
        return list(H_VEC)

    monkeypatch.setattr(hyde, "embed_hypothetical", fake_embed_hyp)
    cfg = {"project_name": "p", "qdrant_url": "http://q",
           "embed": {"ollama_url": "http://o", "ollama_model": "m", "colpali_enabled": False},
           "search": {"top_n": 5, "hybrid": {"enabled": True}}}
    return calls, cfg


def test_hypothetical_blends_dense_lane_and_leaves_bm25_raw(rig):
    calls, cfg = rig
    pipeline.run_search("why are prices stale", cfg, hypothetical="CDN copies expire only after their TTL.")
    (query, dense), = calls["lane"]
    assert query == "why are prices stale"         # BM25 lane: raw query
    assert dense == pytest.approx(BLEND)            # dense lane: blended
    assert calls["hyp"][0][0] == "CDN copies expire only after their TTL."


def test_no_hypothetical_leaves_dense_vector_untouched(rig):
    calls, cfg = rig
    pipeline.run_search("why are prices stale", cfg)
    assert calls["lane"][0][1] == Q_VEC
    assert calls["hyp"] == []


def test_blank_hypothetical_is_ignored(rig):
    calls, cfg = rig
    pipeline.run_search("why are prices stale", cfg, hypothetical="   ")
    assert calls["lane"][0][1] == Q_VEC
    assert calls["hyp"] == []


def test_hypothetical_embed_gets_the_wall_clock_budget(rig):
    calls, cfg = rig
    pipeline.run_search("q", cfg, timeout_s=5, hypothetical="h")
    budget = calls["hyp"][0][1]
    assert budget is not None and 0 < budget <= 5


def test_hypothetical_embed_failure_is_surfaced_not_swallowed(rig, monkeypatch):
    calls, cfg = rig

    def boom(*a, **k):
        raise ConnectionError("ollama down")

    monkeypatch.setattr(hyde, "embed_hypothetical", boom)
    with pytest.raises(RuntimeError, match="hypothetical"):
        pipeline.run_search("q", cfg, hypothetical="h")


def test_dense_only_path_also_receives_the_blend(rig, monkeypatch):
    """Hybrid disabled / legacy unnamed collection: query_points gets the dense vector
    directly, so it must be the blended one too."""
    calls, cfg = rig
    cfg["search"]["hybrid"]["enabled"] = False
    sent = []

    class Resp:
        points = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def query_points(self, **kw):
            sent.append(kw["query"])
            return Resp()

    monkeypatch.setattr(pipeline, "QdrantClient", FakeClient)
    pipeline.run_search("why are prices stale", cfg, hypothetical="h")
    assert sent and sent[0] == pytest.approx(BLEND)


def test_visual_only_search_never_embeds_the_hypothetical(rig, monkeypatch):
    """No text collection -> no text query vector -> nothing to blend (ColPali keeps the raw query)."""
    calls, cfg = rig
    import carta.search.scoped as scoped_mod
    monkeypatch.setattr(scoped_mod, "get_search_collections", lambda cfg, scope: ["p_visual"])
    monkeypatch.setattr(pipeline, "_embed_query_or_raise", lambda q, cfg, colls, timeout=None: None)
    pipeline.run_search("why are prices stale", cfg, hypothetical="h")
    assert calls["hyp"] == []
