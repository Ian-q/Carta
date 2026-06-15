"""Tests for carta.hook.stale_scan — scan core, collectors, and config defaults."""
from carta.config import DEFAULTS


def test_stale_scan_defaults_present():
    sc = DEFAULTS["hooks"]["stale_scan"]
    assert sc["enabled"] is True
    assert sc["block_on_stale"] is False
    assert sc["candidate_threshold"] == 0.65
    assert sc["judge_timeout_s"] == 5
    assert sc["ollama_model"] == "qwen3.5:0.8b"
    assert sc["max_judge_calls"] == 30
