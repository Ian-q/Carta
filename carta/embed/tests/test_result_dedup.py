"""Result de-duplication: distinct docs in the shown top_n (spec 2026-06-17)."""
from carta.embed.pipeline import _dedupe_by_source


def test_dedupe_keeps_first_occurrence_and_order():
    results = [
        {"source": "a.md", "excerpt": "1"},
        {"source": "b.md", "excerpt": "2"},
        {"source": "a.md", "excerpt": "3"},   # dup of a.md
        {"source": "c.md", "excerpt": "4"},
        {"source": "b.md", "excerpt": "5"},   # dup of b.md
    ]
    out = _dedupe_by_source(results)
    assert [h["source"] for h in out] == ["a.md", "b.md", "c.md"]
    assert out[0]["excerpt"] == "1"  # best-ranked occurrence kept


def test_dedupe_keeps_distinct_visual_pages():
    results = [
        {"source": "ds.pdf (page 20)"},
        {"source": "ds.pdf (page 20)"},   # exact dup
        {"source": "ds.pdf (page 31)"},   # different page = distinct result
    ]
    out = _dedupe_by_source(results)
    assert [h["source"] for h in out] == ["ds.pdf (page 20)", "ds.pdf (page 31)"]


def test_dedupe_passes_through_missing_source():
    results = [{"excerpt": "x"}, {"excerpt": "y"}]  # no source key
    out = _dedupe_by_source(results)
    assert len(out) == 2


from unittest.mock import MagicMock


def _run_search_with_merge(monkeypatch, tmp_path, merged, cfg):
    """Drive run_search with _rrf_merge_collections stubbed to return `merged`."""
    import carta.embed.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_rrf_merge_collections", lambda *a, **k: list(merged))
    monkeypatch.setattr(pipeline, "QdrantClient", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(pipeline, "find_config", lambda: str(tmp_path / ".carta" / "config.yaml"))
    monkeypatch.setattr("carta.search.scoped.get_search_collections", lambda cfg, scope: [])
    return pipeline.run_search("query", cfg)


_BASE_CFG = {
    "project_name": "proj",
    "qdrant_url": "http://localhost:6333",
    "embed": {"ollama_url": "http://localhost:11434", "ollama_model": "nomic-embed-text"},
}


def test_run_search_dedupes_results_by_source(monkeypatch, tmp_path):
    merged = [
        {"source": "design.md", "type": "text", "excerpt": "1"},
        {"source": "design.md", "type": "text", "excerpt": "2"},
        {"source": "design.md", "type": "text", "excerpt": "3"},
        {"source": "cts-control.md", "type": "text", "excerpt": "4"},
        {"source": "other.md", "type": "text", "excerpt": "5"},
    ]
    cfg = {**_BASE_CFG, "search": {"top_n": 5, "dedupe_results": True,
                                   "fusion": {"visual_max_ratio": 0.2}}}
    out = _run_search_with_merge(monkeypatch, tmp_path, merged, cfg)
    assert [h["source"] for h in out] == ["design.md", "cts-control.md", "other.md"]


def test_run_search_dedupe_off_preserves_duplicates(monkeypatch, tmp_path):
    merged = [
        {"source": "a.md", "type": "text"},
        {"source": "a.md", "type": "text"},
        {"source": "b.md", "type": "text"},
    ]
    cfg = {**_BASE_CFG, "search": {"top_n": 5, "dedupe_results": False,
                                   "fusion": {"visual_max_ratio": 0.2}}}
    out = _run_search_with_merge(monkeypatch, tmp_path, merged, cfg)
    assert [h["source"] for h in out] == ["a.md", "a.md", "b.md"]


def test_run_search_caps_visual_against_top_n_after_dedup(monkeypatch, tmp_path):
    merged = [
        {"source": "v0", "type": "visual"},
        {"source": "v1", "type": "visual"},
        {"source": "t0", "type": "text"},
        {"source": "t1", "type": "text"},
        {"source": "t2", "type": "text"},
        {"source": "t3", "type": "text"},
    ]
    cfg = {**_BASE_CFG, "search": {"top_n": 5, "dedupe_results": True,
                                   "fusion": {"visual_max_ratio": 0.2}}}  # cap = 1
    out = _run_search_with_merge(monkeypatch, tmp_path, merged, cfg)
    assert sum(1 for h in out if h["type"] == "visual") == 1
    assert len(out) == 5
