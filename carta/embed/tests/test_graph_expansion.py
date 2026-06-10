import carta.embed.pipeline as pipe


def _results():
    # 12 fused hits; the relevant deep neighbour sits at index 10 (rank 11)
    return [{"source": f"docs/d{i}.md", "excerpt": "x", "type": "text"} for i in range(12)]


def test_graph_expansion_promotes_neighbour_into_pool(monkeypatch):
    # Fake graph: top seed docs/d0.md is adjacent to the deep hit docs/d10.md
    fake_adj = {"docs/d0.md": {"docs/d10.md"}, "docs/d10.md": {"docs/d0.md"}}
    monkeypatch.setattr("carta.search.graph.build_related_graph", lambda *a, **k: fake_adj)
    cfg = {"search": {"graph": {"enabled": True, "hops": 1, "seed_count": 3, "candidate_depth": 50}}}
    out = pipe._apply_graph_expansion(_results(), cfg, repo_root="/repo")
    paths = [h["source"] for h in out]
    assert paths[3] == "docs/d10.md"          # promoted to just after the 3 seeds
    assert len(out) == 12                       # nothing dropped


def test_graph_expansion_disabled_does_not_build_graph(monkeypatch):
    calls = {"n": 0}
    def tracked(*a, **k):
        calls["n"] += 1
        return {}
    monkeypatch.setattr("carta.search.graph.build_related_graph", tracked)
    cfg = {"search": {"graph": {"enabled": False}}}
    r = _results()
    out = pipe._apply_graph_expansion(r, cfg, repo_root="/repo")
    assert [h["source"] for h in out] == [h["source"] for h in r]   # identity
    assert calls["n"] == 0                                          # graph never built when disabled


def test_graph_expansion_fails_open_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("graph build failed")
    monkeypatch.setattr("carta.search.graph.build_related_graph", boom)
    cfg = {"search": {"graph": {"enabled": True, "seed_count": 3}}}
    r = _results()
    out = pipe._apply_graph_expansion(r, cfg, repo_root="/repo")
    assert [h["source"] for h in out] == [h["source"] for h in r]   # unchanged, no raise
