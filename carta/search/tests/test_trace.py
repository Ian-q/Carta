import json
import os
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


def test_record_missing_lane_ranks_in_nonempty_hits():
    """Pins the contract: both visual-collection branches and the MCP text
    path produce hits with no `lane_ranks` key at all (not an empty dict).
    A future refactor reintroducing `hits[0]["lane_ranks"]` must fail here."""
    hits = [{"source": "docs/a.md", "score": 0.5, "fused_score": 0.02, "fused_rank": 3}]
    rec = trace.build_trace_record(
        query="q", collections=["c"], hits=hits, zone="inject", judge=None,
        latency_ms=5, score_kind="rrf", rrf_k=2)
    assert rec["lanes"] is None
    assert rec["score"] == 0.5
    assert rec["fused_score"] == 0.02


def test_record_missing_fused_score_in_nonempty_hits():
    """A hit that never went through cross-collection fusion (or a caller
    that only populates the intra-collection score) must not raise."""
    hits = [{"source": "docs/a.md", "score": 0.5}]
    rec = trace.build_trace_record(
        query="q", collections=["c"], hits=hits, zone="silent", judge=False,
        latency_ms=1, score_kind="rrf", rrf_k=2)
    assert rec["fused_score"] is None
    assert rec["score"] == 0.5


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


def test_append_rotates_by_the_records_own_ts_not_now(tmp_path):
    """The monthly file must be chosen from `record["ts"]`, not a fresh
    `_utc_now_iso()` call at append time. Matters near a month boundary, or
    for any future caller that buffers records before flushing (plausible
    for a hook deliberately built not to add latency to prompt submission)."""
    rec = {"ts": "2026-07-31T23:59:59Z", "query": "q"}
    trace.append_trace(tmp_path, rec)
    files = list((tmp_path / ".carta" / "traces").glob("*.jsonl"))
    assert [f.name for f in files] == ["hook-2026-07.jsonl"]


def test_append_still_works_when_record_has_no_ts(tmp_path):
    """append_trace must stay total even for a record lacking `ts` — falls
    back to `_trace_path`'s own now() default."""
    trace.append_trace(tmp_path, {"query": "no ts here"})
    files = list((tmp_path / ".carta" / "traces").glob("hook-*.jsonl"))
    assert len(files) == 1


def test_append_swallows_unwritable_directory(tmp_path):
    """Real OS-level failure mode (not the monkeypatched one): the repo root
    exists but has no write permission, so `.carta/traces` can't be created."""
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)  # read + execute, no write
    try:
        trace.append_trace(locked, {"query": "q"})   # must not raise
        assert not (locked / ".carta").exists()
    finally:
        os.chmod(locked, 0o700)  # restore so tmp_path cleanup can remove it
