"""HyDE blend + hypothetical embed (spec: docs/superpowers/specs/2026-09-21-hyde-hypothetical-query-design.md)."""
import math

import pytest

from carta.search import hyde


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def test_blend_is_unit_norm_equal_weight_mean():
    out = hyde.blend([3.0, 0.0], [0.0, 5.0])
    assert out == pytest.approx([1 / math.sqrt(2), 1 / math.sqrt(2)])
    assert _norm(out) == pytest.approx(1.0)


def test_blend_normalises_inputs_first():
    """Vector magnitude must not decide the mix — only direction."""
    assert hyde.blend([100.0, 0.0], [0.0, 1.0]) == pytest.approx(hyde.blend([1.0, 0.0], [0.0, 1.0]))


def test_blend_identical_directions_returns_that_direction():
    assert hyde.blend([0.0, 2.0], [0.0, 7.0]) == pytest.approx([0.0, 1.0])


def test_usable():
    assert hyde.usable("a passage")
    assert not hyde.usable(None)
    assert not hyde.usable("")
    assert not hyde.usable("   ")


def test_embed_hypothetical_uses_document_prefix(monkeypatch):
    """A hypothetical is shaped like a document, so it is embedded as one."""
    seen = {}

    def fake(text, **kw):
        seen.update(kw, text=text)
        return [1.0, 0.0]

    monkeypatch.setattr(hyde, "get_embedding", fake)
    hyde.embed_hypothetical("cache entries expire after their TTL", "http://o", "m", timeout=2.0)
    assert seen["prefix"] == "search_document: "
    assert seen["text"] == "cache entries expire after their TTL"
    assert seen["timeout"] == 2.0
    assert seen["model"] == "m" and seen["ollama_url"] == "http://o"


def test_embed_hypothetical_omits_timeout_when_none(monkeypatch):
    """requests treats timeout=None as 'wait forever' — never forward it (#106)."""
    seen = {}
    monkeypatch.setattr(hyde, "get_embedding", lambda text, **kw: seen.update(kw) or [1.0])
    hyde.embed_hypothetical("x", "http://o", "m")
    assert "timeout" not in seen
