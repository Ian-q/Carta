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
