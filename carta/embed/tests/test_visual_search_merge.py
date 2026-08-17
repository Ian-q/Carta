"""Regression tests for cross-collection result fusion in run_search.

Bug: text (cosine/RRF, ~0-1) and visual (ColPali MaxSim, ~10-40) hits were merged
by raw score, so visual always won every slot — enabling colpali_enabled dropped
recall to 0. The fix fuses by rank (RRF across collections), which is scale-free.
"""
from unittest.mock import MagicMock

import pytest

from carta.embed.pipeline import _rrf_merge_collections


def _text(slug, score):
    return {"score": score, "source": slug, "excerpt": slug, "type": "text"}


def _visual(slug, score):
    return {"score": score, "source": slug, "excerpt": f"[Visual result] {slug}", "type": "visual"}


def test_high_scored_visual_does_not_crowd_out_text():
    # Text hits carry tiny cosine/RRF scores; visual hits carry huge MaxSim scores.
    text = [_text("t0", 0.55), _text("t1", 0.50), _text("t2", 0.45),
            _text("t3", 0.40), _text("t4", 0.35)]
    visual = [_visual("v0", 34.0), _visual("v1", 30.0), _visual("v2", 25.0)]

    merged = _rrf_merge_collections([text, visual], top_n=5)

    types = [m["type"] for m in merged]
    # The catastrophic failure was zero text results surviving. At minimum the
    # top text hit must be present, and text must not be fully crowded out.
    assert "text" in types, f"text crowded out entirely: {types}"
    assert merged[0]["source"] == "t0", f"top text hit lost its rank-0 slot: {merged[0]}"
    assert types.count("text") >= 2, f"too few text results survived: {types}"


def test_collection_order_breaks_rrf_ties_text_first():
    # Equal ranks -> equal RRF; text collection listed first must win the tie.
    text = [_text("t0", 0.01)]      # tiny score
    visual = [_visual("v0", 99.0)]  # huge score
    merged = _rrf_merge_collections([text, visual], top_n=2)
    assert [m["source"] for m in merged] == ["t0", "v0"]


def test_truncates_to_top_n():
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(10)]
    merged = _rrf_merge_collections([text], top_n=3)
    assert len(merged) == 3
    assert [m["source"] for m in merged] == ["t0", "t1", "t2"]


def test_single_collection_preserves_order():
    visual = [_visual("v0", 30), _visual("v1", 20), _visual("v2", 10)]
    merged = _rrf_merge_collections([visual], top_n=5)
    assert [m["source"] for m in merged] == ["v0", "v1", "v2"]


def test_cap_is_noop_without_visual_lane():
    # A capping ratio must not change output when there are no visual hits.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(6)]
    capped = _rrf_merge_collections([text], top_n=5, visual_max_ratio=0.2)
    uncapped = _rrf_merge_collections([text], top_n=5, visual_max_ratio=1.0)
    assert [m["source"] for m in capped] == [m["source"] for m in uncapped]
    assert [m["source"] for m in capped] == ["t0", "t1", "t2", "t3", "t4"]


def test_visual_cap_limits_visual_share():
    # cap = round(0.2 * 5) = 1 -> exactly one visual survives, text keeps the rest.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(5)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(5)]
    merged = _rrf_merge_collections([text, visual], top_n=5, visual_max_ratio=0.2)
    types = [m["type"] for m in merged]
    assert types.count("visual") == 1
    assert types.count("text") == 4
    # RRF order preserved among admitted hits (text-first tie at rank 0).
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "t2", "t3"]


def test_overflow_backfills_when_text_exhausted():
    # Few text hits, many visual, cap = 1: pool must still fill to top_n from the
    # diverted (overflow) visual hits, in RRF order.
    text = [_text("t0", 0.5), _text("t1", 0.49)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(6)]
    merged = _rrf_merge_collections([text, visual], top_n=5, visual_max_ratio=0.2)
    assert len(merged) == 5
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "v1", "v2"]


def test_admitted_hits_keep_rrf_order():
    # cap = round(0.25 * 6) = 2: two visual admitted, interleave order preserved, no backfill.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(4)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(4)]
    merged = _rrf_merge_collections([text, visual], top_n=6, visual_max_ratio=0.25)
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "v1", "t2", "t3"]


def test_ratio_one_disables_cap():
    # visual_max_ratio = 1.0 (the function default) -> full RRF interleave, unchanged.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(3)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(3)]
    merged = _rrf_merge_collections([text, visual], top_n=6, visual_max_ratio=1.0)
    assert [m["source"] for m in merged] == ["t0", "v0", "t1", "v1", "t2", "v2"]


def test_ratio_zero_excludes_visual_when_text_fills_pool():
    # cap = round(0.0 * 5) = 0 -> no visual admitted while text can fill the pool.
    text = [_text(f"t{i}", 0.5 - i * 0.01) for i in range(5)]
    visual = [_visual(f"v{i}", 30 - i) for i in range(3)]
    merged = _rrf_merge_collections([text, visual], top_n=5, visual_max_ratio=0.0)
    assert all(m["type"] == "text" for m in merged)
    assert [m["source"] for m in merged] == ["t0", "t1", "t2", "t3", "t4"]


def test_merge_writes_fused_score_and_rank_onto_hits():
    """The cross-collection fusion decides ordering, so its score must be on the
    hit. Before this, hits carried the intra-collection score, which did not
    determine their rank."""
    import carta.embed.pipeline as pipeline

    a = {"source": "a.md", "score": 0.9}
    b = {"source": "b.md", "score": 0.1}
    out = pipeline._rrf_merge_collections([[a], [b]], top_n=2, k=60)

    assert out[0]["fused_rank"] == 0
    assert out[1]["fused_rank"] == 1
    assert out[0]["fused_score"] == pytest.approx(1.0 / 61)
    # intra-collection score preserved, not clobbered
    assert out[0]["score"] == 0.9


def test_run_search_forwards_configured_visual_max_ratio(monkeypatch, tmp_path):
    # With dedupe on (default) the configured ratio is applied at the FINAL visual
    # cap (the merge runs uncapped, deferring the cap to after dedup).
    import carta.embed.pipeline as pipeline

    captured = {}
    monkeypatch.setattr(pipeline, "_rrf_merge_collections", lambda *a, **k: [])

    def fake_cap(ordered, limit, visual_max_ratio=1.0):
        captured["ratio"] = visual_max_ratio
        return list(ordered)[:limit]

    monkeypatch.setattr(pipeline, "_apply_visual_cap", fake_cap)
    monkeypatch.setattr(pipeline, "QdrantClient", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(pipeline, "find_config", lambda: str(tmp_path / ".carta" / "config.yaml"))
    # No collections -> the per-collection loop is skipped and the merge is still called.
    monkeypatch.setattr("carta.search.scoped.get_search_collections", lambda cfg, scope: [])

    cfg = {
        "project_name": "proj",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "nomic-embed-text"},
        "search": {"top_n": 5, "fusion": {"visual_max_ratio": 0.34}},
    }
    pipeline.run_search("query", cfg)
    assert captured["ratio"] == 0.34
