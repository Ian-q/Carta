"""run_search's per-stage snapshots (#122).

`--trace` could see only the final result list, so a document that was retrieved,
fused and then narrowed out was indistinguishable from one that was never in the
corpus — and the trace confidently blamed ingestion. These pin the out-param that
carries the intermediate pools out to the tracer.
"""
from unittest.mock import MagicMock

import carta.embed.pipeline as pipeline


_BASE_CFG = {
    "project_name": "proj",
    "qdrant_url": "http://localhost:6333",
    "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "nomic-embed-text"},
}


def _run_search(monkeypatch, tmp_path, merged, cfg=None, **kwargs):
    """Drive run_search with _rrf_merge_collections stubbed to return `merged`."""
    monkeypatch.setattr(pipeline, "_rrf_merge_collections", lambda *a, **k: list(merged))
    monkeypatch.setattr(pipeline, "QdrantClient", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(pipeline, "find_config", lambda: str(tmp_path / ".carta" / "config.yaml"))
    monkeypatch.setattr("carta.search.scoped.get_search_collections", lambda cfg, scope: [])
    return pipeline.run_search("query", cfg or _BASE_CFG, **kwargs)


def _docs(n, prefix="d"):
    return [{"source": f"{prefix}{i}.md", "type": "text", "excerpt": str(i)} for i in range(n)]


def test_trace_stages_records_a_doc_that_was_fused_then_truncated(monkeypatch, tmp_path):
    """The defect in one assertion: `TARGET.md` is in the fused pool and not in
    the returned list. Only the stage snapshots can tell those two facts apart."""
    merged = _docs(5) + [{"source": "TARGET.md", "type": "text", "excerpt": "t"}]
    stages = {}
    results = _run_search(monkeypatch, tmp_path, merged, trace_stages=stages)

    assert "TARGET.md" not in [h["source"] for h in results]
    assert "TARGET.md" in [h["source"] for h in stages["fused"]]
    assert "TARGET.md" not in [h["source"] for h in stages["final"]]


def test_trace_stages_final_matches_the_returned_list(monkeypatch, tmp_path):
    """`final` must be what the caller actually got, or the trace's FINAL row
    describes a different list than the one on screen."""
    stages = {}
    results = _run_search(monkeypatch, tmp_path, _docs(9), trace_stages=stages)
    assert [h["source"] for h in stages["final"]] == [h["source"] for h in results]


def test_trace_stages_omits_stages_that_did_not_run(monkeypatch, tmp_path):
    """Dedup and rerank are both optional. A stage that never executed must be
    absent, not recorded as empty — the tracer infers "lost at X" from the next
    *recorded* stage, so a phantom empty stage would blame the wrong narrowing."""
    cfg = dict(_BASE_CFG)
    cfg["search"] = {"top_n": 5, "dedupe_results": False}
    stages = {}
    _run_search(monkeypatch, tmp_path, _docs(9), cfg=cfg, trace_stages=stages)

    assert "post_dedupe" not in stages
    assert "post_rerank" not in stages          # rerank is opt-in and off here
    assert "retrieved" in stages and "fused" in stages and "final" in stages


def test_plain_top_n_loss_is_not_blamed_on_the_visual_cap(monkeypatch, tmp_path):
    """The dominant loss must be named correctly, end to end.

    `_apply_visual_cap` is not only a quota filter — it hard-truncates at `limit`.
    Snapshotting its output as a stage therefore made it swallow plain top_n
    truncation, so a pure-text document that merely ranked 6th was reported as
    "dropped by the visual cap", pointing the operator at `visual_max_ratio`,
    which had nothing to do with it. That is the same confidently-wrong-diagnosis
    class this whole change exists to remove.

    Driven through the REAL run_search, because the defect lives in the shape of
    the stages dict that run_search actually produces under shipped defaults —
    a hand-built dict cannot expose it.
    """
    from carta.search.trace import format_trace

    cfg = dict(_BASE_CFG)
    cfg["search"] = {"top_n": 5, "dedupe_results": True,
                     "fusion": {"visual_max_ratio": 0.2}}
    merged = _docs(5) + [{"source": "TARGET.md", "type": "text", "excerpt": "t"}]

    stages = {}
    results = _run_search(monkeypatch, tmp_path, merged, cfg=cfg, trace_stages=stages)
    assert "TARGET.md" not in [h["source"] for h in results]

    out = format_trace(results, "TARGET", "q", ["c"], stages=stages)
    assert "visual cap" not in out, (
        f"pure-text rank loss blamed on the visual cap:\n{out}"
    )
    assert "top_n" in out


def test_visual_quota_casualty_is_still_named_as_the_visual_cap(monkeypatch, tmp_path):
    """The converse: a visual hit the quota genuinely diverted must say so."""
    from carta.search.trace import format_trace

    cfg = dict(_BASE_CFG)
    cfg["search"] = {"top_n": 5, "dedupe_results": True,
                     "fusion": {"visual_max_ratio": 0.2}}
    # Cap = round(0.2 * 5) = 1, so only the first visual hit is admitted.
    merged = [
        {"source": "v0.pdf (page 0)", "type": "visual", "excerpt": "v"},
        {"source": "VICTIM.pdf (page 1)", "type": "visual", "excerpt": "v"},
    ] + _docs(4)

    stages = {}
    results = _run_search(monkeypatch, tmp_path, merged, cfg=cfg, trace_stages=stages)
    assert "VICTIM.pdf (page 1)" not in [h["source"] for h in results]

    out = format_trace(results, "VICTIM", "q", ["c"], stages=stages)
    assert "visual cap" in out, f"visual-quota casualty misattributed:\n{out}"


def test_candidate_pool_slice_is_not_blamed_on_the_reranker(monkeypatch, tmp_path):
    """A doc cut by `rerank.candidate_pool` was never scored by the cross-encoder.

    Reporting it as a reranker loss points at reranker quality when the remedy is
    raising `candidate_pool` — the wrong knob, which is the defect class here.
    """
    from carta.search.trace import format_trace

    cfg = dict(_BASE_CFG)
    cfg["search"] = {"top_n": 5, "dedupe_results": True,
                     "rerank": {"enabled": True, "candidate_pool": 3}}
    merged = _docs(3) + [{"source": "TARGET.md", "type": "text", "excerpt": "t"}]

    # rerank_dispatch is stubbed to a pass-through so only the slice narrows.
    import carta.search.rerank as rerank_mod
    monkeypatch.setattr(rerank_mod, "rerank_dispatch",
                        lambda q, pool, **kw: list(pool))

    stages = {}
    results = _run_search(monkeypatch, tmp_path, merged, cfg=cfg, trace_stages=stages)
    assert "TARGET.md" not in [h["source"] for h in results]

    out = format_trace(results, "TARGET", "q", ["c"], stages=stages)
    assert "candidate_pool" in out, f"slice loss blamed on the reranker:\n{out}"


def test_collections_queried_excludes_one_that_did_not_answer(monkeypatch, tmp_path):
    """The trace's "collections" line must name what actually answered.

    The CLI previously recomputed the list from config, so a collection that
    404'd or was skipped as not-ready was still reported as searched — the trace
    asserting something it had not checked, which is the whole defect class here.
    """
    def fake_query_points(collection_name=None, **kw):
        if collection_name == "proj_missing":
            raise RuntimeError("404 collection not found")
        return MagicMock(points=[])

    client = MagicMock()
    client.query_points.side_effect = fake_query_points

    monkeypatch.setattr(pipeline, "QdrantClient", lambda *a, **kw: client)
    monkeypatch.setattr(pipeline, "find_config", lambda: str(tmp_path / ".carta" / "config.yaml"))
    monkeypatch.setattr("carta.search.scoped.get_search_collections",
                        lambda cfg, scope: ["proj_doc", "proj_missing"])
    monkeypatch.setattr(pipeline, "get_embedding", lambda *a, **k: [0.0] * 768)

    stages = {}
    pipeline.run_search("query", _BASE_CFG, trace_stages=stages)

    assert stages["collections_queried"] == ["proj_doc"]


def test_run_search_without_trace_stages_is_unaffected(monkeypatch, tmp_path):
    """The out-param is opt-in: the hook's submit-blocking path passes nothing
    and must allocate nothing."""
    results = _run_search(monkeypatch, tmp_path, _docs(9))
    assert [h["source"] for h in results] == [f"d{i}.md" for i in range(5)]
