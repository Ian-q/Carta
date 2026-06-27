"""Tests for carta.hook.judge.ollama_yesno."""
from unittest.mock import MagicMock, patch

from carta.hook.judge import ollama_yesno
from carta.hook import judge


def _resp(content):
    m = MagicMock()
    m.json.return_value = {"message": {"content": content}}
    return m


def test_ollama_yesno_true_on_yes():
    with patch("requests.post", return_value=_resp("Yes, replaced")):
        assert ollama_yesno("http://x", "m", "sys", "usr", timeout_s=1) is True


def test_ollama_yesno_false_on_no():
    with patch("requests.post", return_value=_resp("no")):
        assert ollama_yesno("http://x", "m", "sys", "usr", timeout_s=1) is False


def test_ollama_yesno_none_on_error():
    with patch("requests.post", side_effect=Exception("connection refused")):
        assert ollama_yesno("http://x", "m", "sys", "usr", timeout_s=1) is None


def test_ollama_yesno_sends_system_and_user():
    with patch("requests.post", return_value=_resp("yes")) as mock_post:
        ollama_yesno("http://x", "mymodel", "SYS", "USR", timeout_s=2)
    payload = mock_post.call_args[1]["json"]
    assert payload["model"] == "mymodel"
    roles = {m["role"]: m["content"] for m in payload["messages"]}
    assert roles["system"] == "SYS"
    assert roles["user"] == "USR"
    assert payload["stream"] is False


def test_extract_json_plain_object():
    assert judge._extract_json('{"conflict": true}') == {"conflict": True}


def test_extract_json_wrapped_in_prose():
    assert judge._extract_json('Sure: {"conflict": false} done') == {"conflict": False}


def test_extract_json_nested_balanced():
    assert judge._extract_json('{"a": {"b": 1}} trailing') == {"a": {"b": 1}}


def test_extract_json_returns_none_on_garbage():
    assert judge._extract_json("no json here") is None
    assert judge._extract_json("") is None


def test_ollama_json_returns_dict(monkeypatch):
    class _Resp:
        def json(self):
            return {"message": {"content": '{"conflict": true}'}}
    monkeypatch.setattr("carta.hook.judge.requests.post", lambda *a, **k: _Resp())
    assert judge.ollama_json("http://x", "m", "sys", "usr", timeout_s=1) == {"conflict": True}


def test_ollama_json_none_on_http_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("carta.hook.judge.requests.post", _boom)
    assert judge.ollama_json("http://x", "m", "sys", "usr", timeout_s=1) is None
