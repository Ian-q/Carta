"""Tests for the hook's bounded search budget (issue #106).

The proactive-recall hook blocks prompt submission. With a local backend an
outage is instant (ECONNREFUSED), so the underlying 60s embed timeout never
binds. With a remote backend a dead peer drops packets silently, so without a
budget every prompt stalls for the full timeout. These tests pin the budget and,
just as importantly, pin that callers who pass no budget are unaffected.
"""

from unittest.mock import MagicMock

import carta.embed.embed as embed_mod
import carta.embed.pipeline as pipeline


def _ok_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"embedding": [0.0] * 768}
    return resp


def test_get_embedding_default_timeout_is_60(monkeypatch):
    """Unchanged default: every existing caller keeps the 60s ceiling."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _ok_response()

    monkeypatch.setattr(embed_mod.requests, "post", fake_post)
    embed_mod.get_embedding("hello")
    assert captured["timeout"] == 60


def test_get_embedding_honours_explicit_timeout(monkeypatch):
    """An explicit timeout reaches requests.post."""
    captured = {}

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _ok_response()

    monkeypatch.setattr(embed_mod.requests, "post", fake_post)
    embed_mod.get_embedding("hello", timeout=2.5)
    assert captured["timeout"] == 2.5


# ---------------------------------------------------------------------------
# _embed_query_or_raise forwards the budget
# ---------------------------------------------------------------------------

def test_embed_query_or_raise_forwards_timeout(monkeypatch):
    """The budget must reach get_embedding, not stop at the wrapper."""
    captured = {}

    def fake_get_embedding(text, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return [0.0] * 768

    monkeypatch.setattr(pipeline, "get_embedding", fake_get_embedding)
    cfg = {"embed": {"ollama_url": "http://x", "ollama_model": "m"}}
    pipeline._embed_query_or_raise("q", cfg, ["proj_doc"], timeout=1.5)
    assert captured["timeout"] == 1.5


def test_embed_query_or_raise_default_timeout_is_none(monkeypatch):
    """Without a budget, no timeout kwarg is forced on get_embedding.

    Passing timeout=None explicitly would override get_embedding's own 60s
    default with None (requests treats None as 'wait forever'), so the
    no-budget path must omit the kwarg entirely.
    """
    captured = {}

    def fake_get_embedding(text, **kwargs):
        captured["timeout"] = kwargs.get("timeout", "ABSENT")
        return [0.0] * 768

    monkeypatch.setattr(pipeline, "get_embedding", fake_get_embedding)
    cfg = {"embed": {"ollama_url": "http://x", "ollama_model": "m"}}
    pipeline._embed_query_or_raise("q", cfg, ["proj_doc"])
    assert captured["timeout"] == "ABSENT"


# ---------------------------------------------------------------------------
# run_search wall-clock budget
# ---------------------------------------------------------------------------

def _make_timeout_cfg():
    return {
        "project_name": "test_proj",
        "qdrant_url": "http://localhost:6333",
        "embed": {"ollama_url": "http://x", "ollama_model": "m"},
        "search": {"top_n": 5, "dedupe_results": False},
    }


def _patch_search_deps(monkeypatch, *, collections, client_timeouts,
                       embed_timeouts, queried, clock=None):
    """Patch run_search's deps, recording the timeouts each dep receives."""
    monkeypatch.setattr(pipeline, "find_config", lambda: "/fake/.carta/config.yaml")

    import carta.search.scoped as scoped_mod
    monkeypatch.setattr(scoped_mod, "get_search_collections",
                        lambda cfg, scope: list(collections))

    def fake_embed(query, cfg, colls, timeout=None):
        embed_timeouts.append(timeout)
        return [0.0] * 768

    monkeypatch.setattr(pipeline, "_embed_query_or_raise", fake_embed)
    monkeypatch.setattr(pipeline, "collection_is_hybrid", lambda c, n: False)

    resp = MagicMock()
    resp.points = []

    class FakeQdrantClient:
        def __init__(self, *a, **k):
            client_timeouts.append(k.get("timeout"))

        def query_points(self, **kwargs):
            queried.append(kwargs.get("collection_name"))
            return resp

    monkeypatch.setattr(pipeline, "QdrantClient", FakeQdrantClient)

    if clock is not None:
        # Patch pipeline's `time` reference, NOT the real time module. Doing
        # `setattr(pipeline.time, "monotonic", clock)` would mutate the stdlib
        # module process-wide for the duration, handing the fake clock to pytest
        # internals and anything else running. The shim delegates everything
        # except monotonic.
        import time as _real_time

        class _FakeTime:
            monotonic = staticmethod(clock)

            def __getattr__(self, name):
                return getattr(_real_time, name)

        monkeypatch.setattr(pipeline, "time", _FakeTime())


def test_run_search_without_budget_keeps_legacy_timeouts(monkeypatch):
    """REGRESSION GUARD: no timeout_s means 10s Qdrant and no embed clamp.

    This is the load-bearing test of the whole change — it pins the promise that
    CLI, MCP and eval are untouched.
    """
    client_timeouts, embed_timeouts, queried = [], [], []
    _patch_search_deps(monkeypatch, collections=["test_proj_doc"],
                       client_timeouts=client_timeouts,
                       embed_timeouts=embed_timeouts, queried=queried)

    pipeline.run_search("q", _make_timeout_cfg())

    assert client_timeouts == [10], "non-hook callers must keep the 10s client"
    assert embed_timeouts == [None], "non-hook callers must not clamp the embed"


def test_run_search_budget_clamps_both_calls(monkeypatch):
    """With a budget, both the embed and the Qdrant client are bounded by it."""
    client_timeouts, embed_timeouts, queried = [], [], []
    _patch_search_deps(monkeypatch, collections=["test_proj_doc"],
                       client_timeouts=client_timeouts,
                       embed_timeouts=embed_timeouts, queried=queried)

    pipeline.run_search("q", _make_timeout_cfg(), timeout_s=3)

    assert embed_timeouts[0] is not None and embed_timeouts[0] <= 3
    assert client_timeouts[0] is not None and client_timeouts[0] <= 3


def test_run_search_budget_exhausted_skips_later_collections(monkeypatch):
    """A budget spent during the first collection stops the loop; no raise."""
    client_timeouts, embed_timeouts, queried = [], [], []
    # First call stamps the deadline at t=0 (so deadline=3); every later call
    # reports t=100, i.e. long past it. Deliberately NOT a fixed tick list —
    # that would silently break if the implementation calls monotonic() a
    # different number of times.
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 100.0

    _patch_search_deps(monkeypatch, collections=["a_doc", "b_doc", "c_doc"],
                       client_timeouts=client_timeouts,
                       embed_timeouts=embed_timeouts, queried=queried,
                       clock=clock)

    results = pipeline.run_search("q", _make_timeout_cfg(), timeout_s=3)

    assert results == [], "exhausted budget returns partial results, not an error"
    assert len(queried) < 3, f"expected the loop to stop early, queried {queried}"
