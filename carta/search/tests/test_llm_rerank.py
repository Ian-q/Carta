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


import requests as _requests


def test_fail_open_on_timeout():
    with patch("carta.search.llm_rerank.requests.post", side_effect=_requests.Timeout()):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=3)
    assert [h["source"] for h in out] == ["a.md", "b.md", "c.md"]


def test_fail_open_on_non_json():
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("not json at all")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["a.md", "b.md"]


def test_out_of_range_and_dupes_filtered_then_filled():
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("[3, 9, 3]")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=4)
    assert [h["source"] for h in out] == ["d.md", "a.md", "b.md", "c.md"]


def test_empty_hits_and_blank_query():
    assert llm_rerank_hits("q", [], model="m", ollama_url="u", top_n=5) == []
    out = llm_rerank_hits("   ", _hits(), model="m", ollama_url="u", top_n=2)
    assert [h["source"] for h in out] == ["a.md", "b.md"]


def test_parses_array_with_trailing_extra_data():
    # Small models sometimes emit a valid array then trailing junk despite format=json.
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("[2, 0] some trailing text")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["c.md", "a.md"]


def test_parses_array_with_leading_prose():
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("Here you go: [1, 0]")):
        out = llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    assert [h["source"] for h in out] == ["b.md", "a.md"]


def test_request_disables_thinking():
    # The default llm_model (qwen3.5:0.8b) is a reasoning model; without think:false
    # the answer goes to message.thinking and message.content stays empty → fail-open.
    with patch("carta.search.llm_rerank.requests.post", return_value=_resp("[0, 1]")) as post:
        llm_rerank_hits("q", _hits(), model="m", ollama_url="http://x:11434", top_n=2)
    payload = post.call_args.kwargs["json"]
    assert payload["think"] is False
