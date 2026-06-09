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
