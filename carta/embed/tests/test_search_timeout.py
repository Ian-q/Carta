"""Tests for the hook's bounded search budget (issue #106).

The proactive-recall hook blocks prompt submission. With a local backend an
outage is instant (ECONNREFUSED), so the underlying 60s embed timeout never
binds. With a remote backend a dead peer drops packets silently, so without a
budget every prompt stalls for the full timeout. These tests pin the budget and,
just as importantly, pin that callers who pass no budget are unaffected.
"""

from unittest.mock import MagicMock

import carta.embed.embed as embed_mod


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
