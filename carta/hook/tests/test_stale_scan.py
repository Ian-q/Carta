"""Tests for carta.hook.stale_scan — scan core, collectors, and config defaults."""
from pathlib import Path

from carta.config import DEFAULTS

from carta.hook.stale_scan import ChangedDoc, StaleFinding, StaleScanResult, _in_doc_scope, _search_cfg


def test_stale_scan_defaults_present():
    sc = DEFAULTS["hooks"]["stale_scan"]
    assert sc["enabled"] is True
    assert sc["block_on_stale"] is False
    assert sc["candidate_threshold"] == 0.65
    assert sc["judge_timeout_s"] == 5
    assert sc["ollama_model"] == "qwen3.5:0.8b"
    assert sc["max_judge_calls"] == 30


def test_dataclasses_construct():
    cd = ChangedDoc(path="docs/a.md", text="hi")
    assert cd.path == "docs/a.md"
    f = StaleFinding(file="docs/a.md", section="## X", snippet="...", candidate_path="docs/b.md", candidate_score=0.9)
    assert f.candidate_score == 0.9
    r = StaleScanResult()
    assert r.findings == [] and r.scanned == 0 and r.judge_calls == 0 and r.skipped_overflow == 0


def test_search_cfg_forces_rerank_and_colpali_off():
    cfg = {"embed": {"colpali_enabled": True}, "search": {"rerank": {"enabled": True}}}
    out = _search_cfg(cfg)
    assert out["embed"]["colpali_enabled"] is False
    assert out["search"]["rerank"]["enabled"] is False


def test_in_doc_scope(tmp_path):
    cfg = {"docs_root": "docs/", "excluded_paths": []}
    assert _in_doc_scope("docs/guide.md", cfg, tmp_path) is True
    assert _in_doc_scope("src/main.py", cfg, tmp_path) is False      # not .md
    assert _in_doc_scope("notes/x.md", cfg, tmp_path) is False        # outside docs_root


# ---------------------------------------------------------------------------
# Task 5: run_stale_scan core
# ---------------------------------------------------------------------------

from carta.hook.stale_scan import run_stale_scan

_CFG = {"embed": {"chunking": {"max_tokens": 400}}, "hooks": {"stale_scan": {"candidate_threshold": 0.65, "max_judge_calls": 30}}}


def _doc(path="docs/a.md"):
    return ChangedDoc(path=path, text="## micro-ROS UART\nThe UART transport uses micro-ROS.\n")


def test_stale_section_with_yes_judge_yields_finding():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "COBS+JSON replaced micro-ROS"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.scanned == 1
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.file == "docs/a.md"
    assert f.candidate_path == "docs/cobs.md"
    assert f.candidate_score == 0.91


def test_related_section_with_no_judge_yields_nothing():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "related but not superseding"}]
    judge = lambda section_text, candidate: False
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []
    assert result.judge_calls == 1


def test_below_threshold_skips_judge():
    calls = []
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.40, "excerpt": "weak match"}]
    judge = lambda section_text, candidate: calls.append(1) or True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []
    assert result.judge_calls == 0
    assert calls == []


def test_judge_none_fails_open():
    search = lambda q: [{"source": "docs/cobs.md", "score": 0.91, "excerpt": "x"}]
    judge = lambda section_text, candidate: None
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=search, judge_fn=judge)
    assert result.findings == []


def test_self_hits_filtered():
    # Only hit is the doc itself -> no external candidate -> no judge, no finding
    search = lambda q: [{"source": "docs/a.md", "score": 0.99, "excerpt": "itself"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), _CFG, [_doc("docs/a.md")], search_fn=search, judge_fn=judge)
    assert result.judge_calls == 0
    assert result.findings == []


def test_search_error_fails_open_per_section():
    def boom(q):
        raise RuntimeError("qdrant down")
    result = run_stale_scan(Path("/repo"), _CFG, [_doc()], search_fn=boom, judge_fn=lambda s, c: True)
    assert result.findings == []


def test_max_judge_calls_cap_reports_overflow():
    cfg = {"embed": {"chunking": {"max_tokens": 400}}, "hooks": {"stale_scan": {"candidate_threshold": 0.65, "max_judge_calls": 1}}}
    docs = [
        ChangedDoc(path="docs/a.md", text="## A\nalpha body text\n"),
        ChangedDoc(path="docs/b.md", text="## B\nbeta body text\n"),
    ]
    search = lambda q: [{"source": "docs/other.md", "score": 0.9, "excerpt": "x"}]
    judge = lambda section_text, candidate: True
    result = run_stale_scan(Path("/repo"), cfg, docs, search_fn=search, judge_fn=judge)
    assert result.judge_calls == 1
    assert result.skipped_overflow == 1
    assert len(result.findings) == 1
