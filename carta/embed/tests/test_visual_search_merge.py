"""Regression tests for cross-collection result fusion in run_search.

Bug: text (cosine/RRF, ~0-1) and visual (ColPali MaxSim, ~10-40) hits were merged
by raw score, so visual always won every slot — enabling colpali_enabled dropped
recall to 0. The fix fuses by rank (RRF across collections), which is scale-free.
"""
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
