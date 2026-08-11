import json
from pathlib import Path
from carta.search import trace


def test_record_captures_lane_ranks_and_zone():
    hits = [{"source": "docs/a.md", "score": 0.7, "fused_score": 0.016,
             "fused_rank": 0, "lane_ranks": {"dense": 0, "sparse": 4}}]
    rec = trace.build_trace_record(
        query="torsion axle spec", collections=["ET-embed_doc"], hits=hits,
        zone="judge", judge=True, latency_ms=412, score_kind="rrf", rrf_k=2,
    )
    assert rec["query"] == "torsion axle spec"
    assert rec["lanes"] == {"dense": 0, "sparse": 4}
    assert rec["zone"] == "judge" and rec["judge"] is True
    assert rec["score_kind"] == "rrf" and rec["rrf_k"] == 2
    assert "ts" in rec


def test_record_handles_zero_hits():
    rec = trace.build_trace_record(
        query="q", collections=[], hits=[], zone="silent", judge=None,
        latency_ms=8, score_kind="rrf", rrf_k=2)
    assert rec["lanes"] is None and rec["score"] is None


def test_append_writes_jsonl_and_creates_dir(tmp_path):
    rec = {"ts": "2026-08-09T00:00:00Z", "query": "q"}
    trace.append_trace(tmp_path, rec)
    files = list((tmp_path / ".carta" / "traces").glob("hook-*.jsonl"))
    assert len(files) == 1
    assert json.loads(files[0].read_text().strip())["query"] == "q"


def test_append_never_raises(tmp_path, monkeypatch):
    """Instrumentation must never break the thing it instruments."""
    monkeypatch.setattr(trace, "_trace_path",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    trace.append_trace(tmp_path, {"query": "q"})   # must not raise
