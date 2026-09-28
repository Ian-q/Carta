from carta.search.rerank import rerank_hits


def test_rerank_reorders_by_cross_encoder_score(monkeypatch):
    import carta.search.rerank as r
    # Fake cross-encoder: score = +1 if "baud" in the chunk text, else 0.
    monkeypatch.setattr(r, "_scores",
                        lambda query, texts, model_name: [1.0 if "baud" in t else 0.0 for t in texts])

    hits = [
        {"text": "unrelated content", "score": 0.99, "file_path": "a.md"},
        {"text": "the bridge runs at 921600 baud", "score": 0.10, "file_path": "b.md"},
    ]
    out = rerank_hits("serial bridge baud", hits, model_name="x", top_n=2)
    assert out[0]["file_path"] == "b.md"            # promoted despite lower original score
    assert out[0]["rerank_score"] == 1.0


def test_rerank_truncates_to_top_n(monkeypatch):
    import carta.search.rerank as r
    monkeypatch.setattr(r, "_scores", lambda q, texts, m: list(range(len(texts))))
    hits = [{"text": str(i), "file_path": f"{i}.md"} for i in range(5)]
    out = rerank_hits("q", hits, model_name="x", top_n=2)
    assert len(out) == 2


from unittest.mock import patch
from carta.search.rerank import rerank_dispatch


def _dispatch_hits():
    return [{"source": "a.md", "excerpt": "x", "type": "text"} for _ in range(3)]


def test_dispatch_routes_to_cross_encoder_by_default():
    rr = {"backend": "cross-encoder", "model": "BAAI/bge-reranker-base"}
    with patch("carta.search.rerank.rerank_hits", return_value=["CE"]) as ce, \
         patch("carta.search.llm_rerank.llm_rerank_hits", return_value=["LLM"]) as llm:
        out = rerank_dispatch("q", _dispatch_hits(), rr_cfg=rr, ollama_url="u", top_n=2)
    assert out == ["CE"]
    ce.assert_called_once()
    llm.assert_not_called()


def test_dispatch_routes_to_llm_when_backend_llm():
    rr = {"backend": "llm", "llm_model": "qwen3.5:0.8b", "llm_timeout_s": 9}
    with patch("carta.search.rerank.rerank_hits", return_value=["CE"]) as ce, \
         patch("carta.search.llm_rerank.llm_rerank_hits", return_value=["LLM"]) as llm:
        out = rerank_dispatch("q", _dispatch_hits(), rr_cfg=rr, ollama_url="u", top_n=2)
    assert out == ["LLM"]
    llm.assert_called_once()
    ce.assert_not_called()
    assert llm.call_args.kwargs["model"] == "qwen3.5:0.8b"
    assert llm.call_args.kwargs["timeout_s"] == 9


# ---------------------------------------------------------------------------
# Durable model cache + public scorer (the hook judge shares this model)
# ---------------------------------------------------------------------------

def test_cross_encoder_cache_dir_is_durable_not_tmp(monkeypatch, tmp_path):
    """fastembed defaults its cache to $TMPDIR/fastembed_cache, which the OS reaps — the
    hook would then re-download an ~80MB model on a prompt-blocking path."""
    import carta.search.rerank as r
    seen = {}

    class FakeEncoder:
        def __init__(self, model_name=None, cache_dir=None, **kw):
            seen["model_name"] = model_name
            seen["cache_dir"] = cache_dir

        def rerank(self, query, texts):
            return [0.0 for _ in texts]

    monkeypatch.setattr(r, "_TextCrossEncoder", FakeEncoder, raising=False)
    monkeypatch.setattr(r, "model_cache_dir", lambda: tmp_path / "models/fastembed")
    r._model.cache_clear()
    r.score_pairs("q", ["a"], "some/model")
    assert seen["cache_dir"] == str(tmp_path / "models/fastembed")
    assert "fastembed_cache" not in seen["cache_dir"]


def test_model_cache_dir_is_under_carta_home():
    from carta.search.rerank import model_cache_dir
    p = model_cache_dir()
    assert p.parts[-3:] == (".carta", "models", "fastembed")


def test_score_pairs_returns_one_score_per_text(monkeypatch):
    import carta.search.rerank as r
    monkeypatch.setattr(r, "_scores", lambda q, texts, m: [float(len(t)) for t in texts])
    assert r.score_pairs("q", ["ab", "abcd"], "m") == [2.0, 4.0]
