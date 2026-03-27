"""Unit tests for carta.hook.hook — three-zone score routing, Ollama judge, fail-open."""

import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(high=0.85, low=0.60, max_results=5, judge_timeout_s=3):
    return {
        "project_name": "test-proj",
        "qdrant_url": "http://localhost:6333",
        "modules": {"proactive_recall": True},
        "proactive_recall": {
            "high_threshold": high,
            "low_threshold": low,
            "max_results": max_results,
            "judge_timeout_s": judge_timeout_s,
            "ollama_model": "qwen2.5:0.5b",
        },
        "embed": {
            "ollama_url": "http://localhost:11434",
            "ollama_model": "nomic-embed-text:latest",
        },
        "search": {"top_n": 5},
    }


def _make_hit(score, source="docs/test.md", excerpt="Some relevant text here."):
    return {"score": score, "source": source, "excerpt": excerpt}


def _stdin(prompt="test query"):
    return io.StringIO(json.dumps({"prompt": prompt}))


def _capture_main():
    """Run main() capturing stdout; return stdout string."""
    from carta.hook.hook import main
    buf = io.StringIO()
    with patch("sys.stdout", buf), patch("sys.__stdout__", buf):
        try:
            main()
        except SystemExit:
            pass
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fast-path: score > high_threshold injects immediately (HOOK-01, HOOK-02)
# ---------------------------------------------------------------------------

def test_fast_path_injects():
    """Score 0.90 > 0.85 high_threshold: inject without calling Ollama."""
    hits = [_make_hit(0.90)]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("how do I configure the embed pipeline")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out = _capture_main()

    assert out.strip(), "Expected JSON output on stdout"
    data = json.loads(out.strip())
    assert "context" in data
    assert "## Relevant documentation" in data["context"]
    assert "docs/test.md" in data["context"]


def test_fast_path_no_ollama_judge():
    """Score > high_threshold must NOT call Ollama (performance)."""
    hits = [_make_hit(0.92)]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post") as mock_post,
    ):
        _capture_main()

    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Noise gate: score < low_threshold discards silently (HOOK-03)
# ---------------------------------------------------------------------------

def test_noise_gate_no_output():
    """Score 0.50 < 0.60 low_threshold: no stdout output."""
    hits = [_make_hit(0.50)]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out = _capture_main()

    assert out.strip() == "", f"Expected no stdout, got: {out!r}"


def test_no_hits_no_output():
    """Empty results: no stdout output."""
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=[]),
    ):
        out = _capture_main()

    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Gray zone: 0.60 <= score <= 0.85, calls Ollama judge (HOOK-04)
# ---------------------------------------------------------------------------

def test_gray_zone_judge_yes_injects():
    """Score 0.75 in gray zone + Ollama says 'yes': inject."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "yes"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out = _capture_main()

    assert out.strip(), "Expected JSON output"
    data = json.loads(out.strip())
    assert "context" in data


def test_gray_zone_judge_no_discards():
    """Score 0.75 in gray zone + Ollama says 'no': no output."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "no"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out = _capture_main()

    assert out.strip() == ""


def test_gray_zone_judge_yes_case_insensitive():
    """'Yes, it is relevant' is treated as yes (D-17 startswith)."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "Yes, it is relevant"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out = _capture_main()

    assert out.strip(), "Expected JSON output for 'Yes, it is relevant'"


def test_gray_zone_judge_maybe_discards():
    """'maybe' does NOT start with 'yes' — should discard."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "maybe"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out = _capture_main()

    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Judge timeout: > judge_timeout_s fails open (HOOK-05)
# ---------------------------------------------------------------------------

def test_judge_timeout_fails_open():
    """Ollama judge sleeping 5s with 3s timeout: injects (fail-open per HOOK-05), completes within 6.5s."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg(judge_timeout_s=3)

    def slow_judge(*args, **kwargs):
        time.sleep(5)
        return True

    t_start = time.time()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("carta.hook.hook._call_ollama_judge", side_effect=slow_judge),
    ):
        out = _capture_main()
    elapsed = time.time() - t_start

    # HOOK-05: timeout is fail-open — inject context rather than discard
    assert out.strip(), "Timeout should inject (fail-open per HOOK-05)"
    data = json.loads(out.strip())
    assert "context" in data
    # Hook logic completes at timeout (3s); thread pool shutdown waits for the
    # slow thread to finish (5s total). Allow up to 6.5s for full cleanup.
    assert elapsed < 6.5, f"Should complete within 6.5s, took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Chunk cap: max 5 chunks regardless of hits count (HOOK-06)
# ---------------------------------------------------------------------------

def test_chunk_cap():
    """8 hits at score 0.90: exactly 5 injected."""
    hits = [_make_hit(0.90, source=f"docs/doc{i}.md", excerpt=f"Excerpt {i}") for i in range(8)]
    cfg = _make_cfg(max_results=5)
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out = _capture_main()

    data = json.loads(out.strip())
    context = data["context"]
    # Each source appears exactly once; only 5 should appear
    injected_count = sum(1 for i in range(8) if f"docs/doc{i}.md" in context)
    assert injected_count == 5, f"Expected 5 chunks, got {injected_count}"


# ---------------------------------------------------------------------------
# Fail-open: run_search raises RuntimeError (Qdrant unreachable)
# ---------------------------------------------------------------------------

def test_fail_open_on_search_error():
    """run_search raises RuntimeError: exit 0, no stdout."""
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=RuntimeError("Qdrant unreachable")),
    ):
        out = _capture_main()

    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Fail-open: invalid JSON on stdin
# ---------------------------------------------------------------------------

def test_fail_open_invalid_json():
    """Invalid JSON stdin: exit 0, no stdout."""
    with patch("sys.stdin", io.StringIO("not valid json {")):
        out = _capture_main()

    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Module disabled: proactive_recall=False exits silently
# ---------------------------------------------------------------------------

def test_module_disabled_no_output():
    """proactive_recall module disabled in config: no output."""
    cfg = _make_cfg()
    cfg["modules"]["proactive_recall"] = False
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search") as mock_search,
    ):
        out = _capture_main()

    mock_search.assert_not_called()
    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Custom config thresholds respected (HOOK-07)
# ---------------------------------------------------------------------------

def test_custom_thresholds_respected():
    """high=0.90, low=0.70: score 0.88 falls in gray zone with custom thresholds."""
    hits = [_make_hit(0.88)]
    cfg = _make_cfg(high=0.90, low=0.70)
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "yes"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=Path("/fake/.carta/config.yaml")),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out = _capture_main()

    # Score 0.88 < 0.90 high threshold, so gray zone — judge says yes => inject
    assert out.strip(), "Expected injection for gray-zone score with custom thresholds"


# ---------------------------------------------------------------------------
# _extract_query tests (D-06)
# ---------------------------------------------------------------------------

def test_extract_query_short_prompt_returns_as_is():
    """Prompt <= 500 chars: returned as-is without calling Ollama."""
    from carta.hook.hook import _extract_query
    cfg = _make_cfg()
    prompt = "short prompt"
    result = _extract_query(prompt, cfg)
    assert result == prompt


def test_extract_query_long_prompt_uses_ollama():
    """Prompt > 500 chars with mocked Ollama: returns compressed query."""
    from carta.hook.hook import _extract_query
    cfg = _make_cfg()
    long_prompt = "x" * 600
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "compressed query"}}
    with patch("requests.post", return_value=mock_resp):
        result = _extract_query(long_prompt, cfg)
    assert result == "compressed query"


def test_extract_query_long_prompt_ollama_failure_fallback():
    """Prompt > 500 chars with Ollama failure: returns last 500 chars."""
    from carta.hook.hook import _extract_query
    cfg = _make_cfg()
    long_prompt = "a" * 200 + "b" * 400  # 600 chars, last 500 = b*400 + a*100? No: last 500 = positions 100-599
    with patch("requests.post", side_effect=Exception("connection refused")):
        result = _extract_query(long_prompt, cfg)
    assert result == long_prompt[-500:]


# ---------------------------------------------------------------------------
# _call_ollama_judge tests (D-15, D-16, D-17, D-18)
# ---------------------------------------------------------------------------

def test_call_ollama_judge_sends_correct_format():
    """Judge sends system message and user message with prompt + excerpts (D-15, D-18)."""
    from carta.hook.hook import _call_ollama_judge
    cfg = _make_cfg()
    hits = [_make_hit(0.75, excerpt="Some excerpt text")]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "yes"}}
    with patch("requests.post", return_value=mock_resp) as mock_post:
        result = _call_ollama_judge("my prompt", hits, cfg)

    assert result is True
    call_kwargs = mock_post.call_args
    payload = call_kwargs[1]["json"] if call_kwargs[1] else call_kwargs[0][1]
    messages = payload["messages"]
    system_msgs = [m for m in messages if m["role"] == "system"]
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert system_msgs, "System message missing"
    assert "yes" in system_msgs[0]["content"].lower() or "relevant" in system_msgs[0]["content"].lower()
    assert user_msgs, "User message missing"
    assert "my prompt" in user_msgs[0]["content"] or "Some excerpt" in user_msgs[0]["content"]


def test_call_ollama_judge_parses_yes():
    """'yes' response returns True (D-17)."""
    from carta.hook.hook import _call_ollama_judge
    cfg = _make_cfg()
    hits = [_make_hit(0.75)]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "yes"}}
    with patch("requests.post", return_value=mock_resp):
        result = _call_ollama_judge("prompt", hits, cfg)
    assert result is True


def test_call_ollama_judge_parses_no():
    """Non-'yes' response returns False."""
    from carta.hook.hook import _call_ollama_judge
    cfg = _make_cfg()
    hits = [_make_hit(0.75)]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "no"}}
    with patch("requests.post", return_value=mock_resp):
        result = _call_ollama_judge("prompt", hits, cfg)
    assert result is False


# ---------------------------------------------------------------------------
# _judge_with_timeout: HOOK-05 fail-open on timeout (returns True)
# ---------------------------------------------------------------------------

def test_judge_timeout_returns_true():
    """TimeoutError in _judge_with_timeout returns True (fail-open per HOOK-05)."""
    import concurrent.futures
    from carta.hook.hook import _judge_with_timeout
    cfg = _make_cfg(judge_timeout_s=1)
    hits = [_make_hit(0.75)]
    with patch("carta.hook.hook._call_ollama_judge", side_effect=concurrent.futures.TimeoutError):
        result = _judge_with_timeout("prompt", hits, cfg, timeout_s=1)
    assert result is True, "TimeoutError must return True (fail-open = inject)"


def test_judge_exception_returns_false():
    """Non-timeout exception in _judge_with_timeout returns False (fail-closed on errors)."""
    from carta.hook.hook import _judge_with_timeout
    cfg = _make_cfg()
    hits = [_make_hit(0.75)]
    with patch("carta.hook.hook._call_ollama_judge", side_effect=RuntimeError("boom")):
        result = _judge_with_timeout("prompt", hits, cfg, timeout_s=3)
    assert result is False, "Non-timeout exceptions must return False"


def test_judge_yes_returns_true():
    """Successful judge returning True propagates correctly."""
    from carta.hook.hook import _judge_with_timeout
    cfg = _make_cfg()
    hits = [_make_hit(0.75)]
    with patch("carta.hook.hook._call_ollama_judge", return_value=True):
        result = _judge_with_timeout("prompt", hits, cfg, timeout_s=3)
    assert result is True


def test_judge_no_returns_false():
    """Successful judge returning False propagates correctly."""
    from carta.hook.hook import _judge_with_timeout
    cfg = _make_cfg()
    hits = [_make_hit(0.75)]
    with patch("carta.hook.hook._call_ollama_judge", return_value=False):
        result = _judge_with_timeout("prompt", hits, cfg, timeout_s=3)
    assert result is False
