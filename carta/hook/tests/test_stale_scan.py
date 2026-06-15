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
