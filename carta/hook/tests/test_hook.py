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
            "ollama_model": "qwen3.5:0.8b",
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


def _capture_main_full():
    """Run main() capturing stdout; return (stdout, exit_code).

    Fail-open means "exits 0", not merely "doesn't crash" — a regression to
    sys.exit(1) anywhere in _run would still print nothing and would still
    pass every test that only checks stdout. Callers that care about the
    fail-open guarantee itself (not just its side effect on stdout) should
    assert on the returned code, not just infer it from silence.
    """
    from carta.hook.hook import main
    buf = io.StringIO()
    code = 0
    with patch("sys.stdout", buf), patch("sys.__stdout__", buf):
        try:
            main()
        except SystemExit as e:
            code = e.code if e.code is not None else 0
    return buf.getvalue(), code


def _capture_main():
    """Run main() capturing stdout; return stdout string only.

    Thin wrapper over _capture_main_full for the majority of tests that only
    care about the injected (or absent) context block. Use
    _capture_main_full directly when the exit code itself is under test.
    """
    out, _ = _capture_main_full()
    return out


# ---------------------------------------------------------------------------
# Fast-path: score > high_threshold injects immediately (HOOK-01, HOOK-02)
# ---------------------------------------------------------------------------

def test_fast_path_injects(tmp_path):
    """Score 0.90 > 0.85 high_threshold: inject without calling Ollama."""
    hits = [_make_hit(0.90)]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("how do I configure the embed pipeline")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out, code = _capture_main_full()

    assert code == 0, "the hook must always exit 0, even on the inject path"
    assert out.strip(), "Expected JSON output on stdout"
    data = json.loads(out.strip())
    assert "context" in data
    assert "## Relevant documentation" in data["context"]
    assert "docs/test.md" in data["context"]


def test_fast_path_no_ollama_judge(tmp_path):
    """Score > high_threshold must NOT call Ollama (performance)."""
    hits = [_make_hit(0.92)]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post") as mock_post,
    ):
        _capture_main()

    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Noise gate: score < low_threshold discards silently (HOOK-03)
# ---------------------------------------------------------------------------

def test_noise_gate_no_output(tmp_path):
    """Score 0.50 < 0.60 low_threshold: no stdout output."""
    hits = [_make_hit(0.50)]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out, code = _capture_main_full()

    assert code == 0, "silent zone must still exit 0"
    assert out.strip() == "", f"Expected no stdout, got: {out!r}"


def test_no_hits_no_output(tmp_path):
    """Empty results: no stdout output."""
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=[]),
    ):
        out, code = _capture_main_full()

    assert code == 0
    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Gray zone: 0.60 <= score <= 0.85, calls Ollama judge (HOOK-04)
# ---------------------------------------------------------------------------

def test_gray_zone_judge_yes_injects(tmp_path):
    """Score 0.75 in gray zone + Ollama says 'yes': inject."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "yes"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out, code = _capture_main_full()

    assert code == 0, "the judge-yes inject path must still exit 0"
    assert out.strip(), "Expected JSON output"
    data = json.loads(out.strip())
    assert "context" in data


def test_gray_zone_judge_no_discards(tmp_path):
    """Score 0.75 in gray zone + Ollama says 'no': no output."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "no"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out, code = _capture_main_full()

    assert code == 0
    assert out.strip() == ""


def test_gray_zone_judge_yes_case_insensitive(tmp_path):
    """'Yes, it is relevant' is treated as yes (D-17 startswith)."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "Yes, it is relevant"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out = _capture_main()

    assert out.strip(), "Expected JSON output for 'Yes, it is relevant'"


def test_gray_zone_judge_maybe_discards(tmp_path):
    """'maybe' does NOT start with 'yes' — should discard."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "maybe"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("requests.post", return_value=mock_resp),
    ):
        out = _capture_main()

    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Judge timeout: > judge_timeout_s skips injection (HOOK-05)
# ---------------------------------------------------------------------------

def test_judge_timeout_skips_injection(tmp_path):
    """Ollama judge sleeping 5s with 3s timeout: skips injection (HOOK-05: no
    injection on timeout, prompt proceeds), completes within 6.5s."""
    hits = [_make_hit(0.75)]
    cfg = _make_cfg(judge_timeout_s=3)

    def slow_judge(*args, **kwargs):
        time.sleep(5)
        return True

    t_start = time.time()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("carta.hook.hook._call_ollama_judge", side_effect=slow_judge),
    ):
        out = _capture_main()
    elapsed = time.time() - t_start

    # HOOK-05: on timeout, do NOT inject the unvetted gray-zone hits — stay silent.
    assert out.strip() == "", "Timeout should skip injection (HOOK-05: no injection on timeout)"
    # Hook logic completes at timeout (3s); thread pool shutdown waits for the
    # slow thread to finish (5s total). Allow up to 6.5s for full cleanup.
    assert elapsed < 6.5, f"Should complete within 6.5s, took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Chunk cap: max 5 chunks regardless of hits count (HOOK-06)
# ---------------------------------------------------------------------------

def test_proactive_recall_search_is_text_only(tmp_path):
    """The per-prompt hook must not trigger the heavy ColPali visual path.

    Regression: with colpali_enabled auto-default, run_search auto-searches the
    _visual collection and loads ColPali (~9s) on every prompt. The hook must
    pass a text-only cfg (colpali_enabled disabled) to run_search.
    """
    cfg = _make_cfg()
    cfg["embed"]["colpali_enabled"] = None  # auto: a normal search would load ColPali
    captured = {}

    def fake_search(query, c, *a, **k):
        captured["cfg"] = c
        return []

    with (
        patch("sys.stdin", _stdin("how do I configure the embed pipeline")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=fake_search),
    ):
        _capture_main()

    assert captured.get("cfg", {}).get("embed", {}).get("colpali_enabled") is False, (
        "proactive-recall hook must disable visual search (text-only) to avoid "
        "loading ColPali on every prompt"
    )


def test_proactive_recall_search_never_reranks(tmp_path):
    """The per-prompt hook must not pay reranker latency.

    Regression: hook.py forced colpali off but passed search.rerank through
    untouched, so enabling search.rerank (e.g. backend=llm, 10s+/call) made
    every prompt submission block on a rerank call. The hook must force
    search.rerank.enabled off in the cfg it passes to run_search.
    """
    cfg = _make_cfg()
    cfg["search"] = {"top_n": 5, "rerank": {"enabled": True, "backend": "llm"}}
    captured = {}

    def fake_search(query, c, *a, **k):
        captured["cfg"] = c
        return []

    with (
        patch("sys.stdin", _stdin("how do I configure the embed pipeline")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=fake_search),
    ):
        _capture_main()

    rr = captured.get("cfg", {}).get("search", {}).get("rerank", {})
    assert rr.get("enabled") is False, (
        "proactive-recall hook must disable reranking — the hook blocks prompt "
        "submission and must never pay rerank latency"
    )
    # The project cfg itself must not be mutated by the override
    assert cfg["search"]["rerank"]["enabled"] is True


def test_chunk_cap(tmp_path):
    """8 hits at score 0.90: exactly 5 injected."""
    hits = [_make_hit(0.90, source=f"docs/doc{i}.md", excerpt=f"Excerpt {i}") for i in range(8)]
    cfg = _make_cfg(max_results=5)
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
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

def test_fail_open_on_search_error(tmp_path):
    """run_search raises RuntimeError: exit 0, no stdout."""
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=RuntimeError("Qdrant unreachable")),
    ):
        out, code = _capture_main_full()

    assert code == 0, "fail-open means exit 0, not just silent stdout"
    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Fail-open: invalid JSON on stdin
# ---------------------------------------------------------------------------

def test_fail_open_invalid_json():
    """Invalid JSON stdin: exit 0, no stdout."""
    with patch("sys.stdin", io.StringIO("not valid json {")):
        out, code = _capture_main_full()

    assert code == 0
    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Module disabled: proactive_recall=False exits silently
# ---------------------------------------------------------------------------

def test_module_disabled_no_output(tmp_path):
    """proactive_recall module disabled in config: no output."""
    cfg = _make_cfg()
    cfg["modules"]["proactive_recall"] = False
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search") as mock_search,
    ):
        out, code = _capture_main_full()

    mock_search.assert_not_called()
    assert code == 0
    assert out.strip() == ""


# ---------------------------------------------------------------------------
# Custom config thresholds respected (HOOK-07)
# ---------------------------------------------------------------------------

def test_custom_thresholds_respected(tmp_path):
    """high=0.90, low=0.70: score 0.88 falls in gray zone with custom thresholds."""
    hits = [_make_hit(0.88)]
    cfg = _make_cfg(high=0.90, low=0.70)
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "yes"}}
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
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


def test_call_ollama_judge_uses_given_timeout():
    """The inner Ollama timeout must track the caller's budget, not a hardcoded 4s.
    A hardcoded inner > the outer thread budget lets the hook block past the
    configured judge_timeout_s (ThreadPoolExecutor.__exit__ waits for the thread)."""
    from carta.hook.hook import _call_ollama_judge
    cfg = _make_cfg()
    hits = [_make_hit(0.75)]
    captured = {}

    def fake_yesno(url, model, system, user, *, timeout_s):
        captured["timeout_s"] = timeout_s
        return True

    with patch("carta.hook.judge.ollama_yesno", fake_yesno):
        _call_ollama_judge("p", hits, cfg, timeout_s=2)
    assert captured["timeout_s"] == 2


def test_judge_with_timeout_passes_budget_to_inner():
    """_judge_with_timeout forwards its budget to the inner judge so inner <= outer."""
    from carta.hook.hook import _judge_with_timeout
    cfg = _make_cfg()
    captured = {}

    def fake_call(prompt, hits, cfg, timeout_s):
        captured["timeout_s"] = timeout_s
        return False

    with patch("carta.hook.hook._call_ollama_judge", fake_call):
        _judge_with_timeout("p", [], cfg, timeout_s=2)
    assert captured["timeout_s"] == 2


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
# _judge_with_timeout: HOOK-05 no injection on timeout (returns False)
# ---------------------------------------------------------------------------

def test_judge_timeout_returns_false():
    """TimeoutError in _judge_with_timeout returns False (HOOK-05: no injection on timeout)."""
    import concurrent.futures
    from carta.hook.hook import _judge_with_timeout
    cfg = _make_cfg(judge_timeout_s=1)
    hits = [_make_hit(0.75)]
    with patch("carta.hook.hook._call_ollama_judge", side_effect=concurrent.futures.TimeoutError):
        result = _judge_with_timeout("prompt", hits, cfg, timeout_s=1)
    assert result is False, "TimeoutError must return False (no injection on timeout)"


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


def test_inject_labels_note_hits(tmp_path):
    """Recalled notes are labeled with their type so Claude can tell curated memory
    from plain docs; plain docs stay unlabeled."""
    hits = [
        {"score": 0.92, "source": "docs/quirks/2026-06-11-psu.md",
         "excerpt": "bench PSU must be on", "doc_type": "quirk"},
        {"score": 0.91, "source": "docs/CAN/TOPOLOGY.md",
         "excerpt": "two CAN buses", "doc_type": ""},
    ]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out = _capture_main()
    data = json.loads(out.strip())
    assert "[quirk] docs/quirks/2026-06-11-psu.md" in data["context"]
    assert "Source: docs/CAN/TOPOLOGY.md" in data["context"]


# ---------------------------------------------------------------------------
# Bounded search budget (issue #106)
# ---------------------------------------------------------------------------

def test_hook_passes_search_timeout_to_run_search(tmp_path):
    """The configured budget must reach run_search."""
    captured = {}

    def fake_run_search(query, cfg, **kwargs):
        captured["timeout_s"] = kwargs.get("timeout_s")
        return [_make_hit(0.90)]

    cfg = _make_cfg()
    cfg["proactive_recall"]["search_timeout_s"] = 7
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=fake_run_search),
    ):
        _capture_main()

    assert captured["timeout_s"] == 7


def test_hook_search_timeout_defaults_to_3(tmp_path):
    """Absent config key falls back to 3s, matching judge_timeout_s."""
    captured = {}

    def fake_run_search(query, cfg, **kwargs):
        captured["timeout_s"] = kwargs.get("timeout_s")
        return [_make_hit(0.90)]

    cfg = _make_cfg()
    cfg["proactive_recall"].pop("search_timeout_s", None)
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=fake_run_search),
    ):
        _capture_main()

    assert captured["timeout_s"] == 3


def test_hook_still_fails_open_when_search_raises(tmp_path):
    """Fail-open is non-negotiable: a search error must still exit 0, silently."""
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=TimeoutError("backend down")),
    ):
        out, code = _capture_main_full()

    assert code == 0, "must exit 0, not just stay silent, when the search failed"
    assert out.strip() == "", "must not inject when the search failed"


def test_search_timeout_default_registered_in_config():
    """The key must exist in DEFAULTS so `carta init` writes it."""
    from carta.config import DEFAULTS
    assert DEFAULTS["proactive_recall"]["search_timeout_s"] == 3


# ---------------------------------------------------------------------------
# _gate_zone: rank-and-agreement gate (replaces absolute RRF-score gating)
# ---------------------------------------------------------------------------

def test_gate_injects_when_both_lanes_agree():
    from carta.hook import hook
    hits = [{"lane_ranks": {"dense": 0, "sparse": 1}}]
    assert hook._gate_zone(hits, agree_rank=3) == "inject"


def test_gate_judges_when_only_one_lane_is_confident():
    from carta.hook import hook
    hits = [{"lane_ranks": {"dense": 0, "sparse": 40}}]
    assert hook._gate_zone(hits, agree_rank=3) == "judge"


def test_gate_judges_when_hit_is_in_one_lane_only():
    """The exact case the old gate dropped: rank 0 in one lane scored ~0.5,
    below low_threshold 0.60, and never reached the judge."""
    from carta.hook import hook
    hits = [{"lane_ranks": {"dense": 0, "sparse": None}}]
    assert hook._gate_zone(hits, agree_rank=3) == "judge"


def test_gate_silent_when_neither_lane_is_confident():
    from carta.hook import hook
    hits = [{"lane_ranks": {"dense": 9, "sparse": 12}}]
    assert hook._gate_zone(hits, agree_rank=3) == "silent"


def test_gate_silent_on_no_hits():
    from carta.hook import hook
    assert hook._gate_zone([], agree_rank=3) == "silent"


def test_gate_falls_back_to_score_when_lane_ranks_absent():
    """Non-hybrid collections return cosine scores and no lane ranks; the old
    thresholds remain correct there."""
    from carta.hook import hook
    assert hook._gate_zone([{"score": 0.9}], agree_rank=3,
                           low=0.6, high=0.85) == "inject"
    assert hook._gate_zone([{"score": 0.5}], agree_rank=3,
                           low=0.6, high=0.85) == "silent"


def test_gate_rank_exactly_at_agree_rank_boundary_is_not_confident():
    """Ranks are 0-indexed, so agree_rank=3 covers ranks 0, 1, 2 (the top
    three). A rank of exactly 3 is 4th place and must NOT count as confident
    — otherwise "top-3" would silently mean "top-4"."""
    from carta.hook import hook
    hits = [{"lane_ranks": {"dense": 3, "sparse": 3}}]
    assert hook._gate_zone(hits, agree_rank=3) == "silent"


def test_gate_default_agree_rank_is_three():
    """A config predating the agree_rank key must still behave sanely: the
    function's own default (not a KeyError) governs."""
    from carta.hook import hook
    hits = [{"lane_ranks": {"dense": 0, "sparse": 1}}]
    assert hook._gate_zone(hits) == "inject"


# ---------------------------------------------------------------------------
# agree_rank config wiring (Step 5): default registered, respected end to end
# ---------------------------------------------------------------------------

def test_agree_rank_default_registered_in_config():
    """The key must exist in DEFAULTS so `carta init` writes it and a config
    predating this feature still gets a sane value via the deep-merge."""
    from carta.config import DEFAULTS
    assert DEFAULTS["proactive_recall"]["agree_rank"] == 3


def test_hook_uses_configured_agree_rank_end_to_end(tmp_path):
    """A hit ranked 2nd in both lanes: agrees within agree_rank=5 (inject) but
    not within the default agree_rank=3... here we widen it via config and
    confirm the gate honors the configured value, not just the function
    default."""
    hits = [{"score": 0.5, "source": "docs/test.md", "excerpt": "text",
             "lane_ranks": {"dense": 4, "sparse": 4}}]
    cfg = _make_cfg()
    cfg["proactive_recall"]["agree_rank"] = 5
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        out = _capture_main()

    assert out.strip(), "rank 4 < configured agree_rank=5 in both lanes should inject"


# ---------------------------------------------------------------------------
# _emit_trace wiring: repo_root and collections are threaded explicitly
# (not read off invented cfg["_repo_root"] / cfg["_trace_collections"] keys),
# and trace emission must never break the hook (fail-open).
# ---------------------------------------------------------------------------

def test_hook_emits_trace_record_with_repo_root_and_collections(tmp_path):
    """End-to-end: main() writes one trace record under <repo_root>/.carta/traces,
    where repo_root is derived from cfg_path.parent.parent, and collections come
    from get_search_collections(search_cfg, "repo") — the same helper run_search
    uses, called with the SAME search_cfg run_search was actually given — not
    from any key inside cfg, and not from the raw project cfg.

    _make_cfg() deliberately leaves embed.colpali_enabled unset (auto), the
    normal case for most projects. Regression pin: the hook's own search_cfg
    (step 6) always forces colpali_enabled=False, so the trace's collections
    must be exactly the 3 non-visual collections the search actually covered —
    NOT the 4 you'd get from get_search_collections(cfg, ...) on the raw,
    colpali-auto project cfg. Passing the raw cfg here would claim _visual was
    searched and came up empty, when it was never queried at all."""
    hits = [{"score": 0.90, "source": "docs/test.md", "excerpt": "text",
             "lane_ranks": {"dense": 0, "sparse": 1}}]
    cfg = _make_cfg()
    assert "colpali_enabled" not in cfg["embed"], "test fixture must stay colpali-auto"
    cfg_path = tmp_path / ".carta" / "config.yaml"
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=cfg_path),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
    ):
        _capture_main()

    trace_files = list((tmp_path / ".carta" / "traces").glob("hook-*.jsonl"))
    assert len(trace_files) == 1, "expected exactly one trace file under the real repo root"
    record = json.loads(trace_files[0].read_text().strip().splitlines()[-1])
    assert record["zone"] == "inject"
    assert record["lanes"] == {"dense": 0, "sparse": 1}
    assert record["collections"] == [
        "test-proj_doc", "test-proj_notes", "test-proj_session",
    ], "must match what the hook actually searched (colpali forced off), not raw cfg"


def test_hook_trace_failure_does_not_break_injection(tmp_path):
    """Trace emission must never affect the hook's actual decision or crash it,
    even if append_trace itself somehow raises."""
    hits = [{"score": 0.90, "source": "docs/test.md", "excerpt": "text",
             "lane_ranks": {"dense": 0, "sparse": 1}}]
    cfg = _make_cfg()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", return_value=hits),
        patch("carta.search.trace.append_trace", side_effect=RuntimeError("disk full")),
    ):
        out = _capture_main()

    data = json.loads(out.strip())
    assert "context" in data, "trace failure must not prevent injection"


def test_emit_trace_swallows_collection_errors():
    """_emit_trace must not raise even if get_search_collections raises —
    it must be called inside the trace's exception guard, not outside it."""
    from carta.hook.hook import _emit_trace
    cfg = _make_cfg()
    with patch("carta.search.scoped.get_search_collections", side_effect=ValueError("bad scope")):
        _emit_trace("q", [], "silent", None, time.monotonic(), cfg, Path("/fake"))
    # Reaching here without an exception is the assertion.


def test_hook_returns_within_budget_against_a_blocking_backend(tmp_path):
    """The whole point: a backend that hangs must not hang the prompt.

    Without the budget this blocks for the full 60s embed timeout. run_search is
    patched to block, so this exercises the hook's contract end to end rather
    than the deadline arithmetic already covered in test_search_timeout.py.
    """
    cfg = _make_cfg()
    cfg["proactive_recall"]["search_timeout_s"] = 1

    def blocking_run_search(query, search_cfg, **kwargs):
        budget = kwargs.get("timeout_s")
        assert budget == 1, f"hook must pass its budget down, got {budget}"
        time.sleep(budget)
        raise TimeoutError("backend unreachable")

    start = time.monotonic()
    with (
        patch("sys.stdin", _stdin("query")),
        patch("carta.hook.hook.find_config", return_value=tmp_path / ".carta" / "config.yaml"),
        patch("carta.hook.hook.load_config", return_value=cfg),
        patch("carta.hook.hook.run_search", side_effect=blocking_run_search),
    ):
        out, code = _capture_main_full()
    elapsed = time.monotonic() - start

    assert code == 0, "must exit 0 even when the backend hangs"
    assert out.strip() == "", "must not inject when the backend is unreachable"
    assert elapsed < 5, f"hook took {elapsed:.1f}s; budget was 1s"
