import json
from unittest.mock import patch, MagicMock
from carta.search.llm_rerank import llm_rerank_hits


def _hits():
    return [
        {"source": "a.md", "excerpt": "alpha", "type": "text"},
        {"source": "b.md", "excerpt": "bravo", "type": "text"},
        {"source": "c.md", "excerpt": "charlie", "type": "text"},
        {"source": "d.md", "excerpt": "delta", "type": "text"},
    ]


def _resp(content: str):
    r = MagicMock()
    r.json.return_value = {"message": {"content": content}}
    r.raise_for_status.return_value = None
    return r


def test_reorders_to_llm_index_order_and_truncates():
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("[2, 0, 1, 3]")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["c.md", "a.md"]
    assert out[0]["rerank_score"] > out[1]["rerank_score"]


def test_accepts_object_wrapped_array():
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp('{"order": [1, 0]}')):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["b.md", "a.md"]
