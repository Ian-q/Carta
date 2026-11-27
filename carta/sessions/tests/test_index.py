"""Indexing session records into {project}_sessions: incremental, filterable.

Qdrant is QdrantClient(":memory:"); the Ollama embedder is replaced by a
deterministic fake, so no service is touched.
"""
import hashlib
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from qdrant_client import QdrantClient

from carta.config import DEFAULTS, _deep_merge
from carta.embed.sparse import SparseVec
from carta.sessions import index as sidx
from carta.sessions.project import transcript_slug
from carta.sessions.tests.synth import Transcript, standard_session

DIM = 768


def fake_embedding(text, **kwargs):
    h = hashlib.sha256(text.encode()).digest()
    return [((h[i % len(h)] / 255.0) - 0.5) for i in range(DIM)]


def fake_sparse(text, model_name=None):
    words = sorted({w.lower() for w in text.split()})[:50]
    idx = sorted({int(hashlib.md5(w.encode()).hexdigest()[:6], 16) for w in words})
    return SparseVec(indices=idx, values=[1.0] * len(idx))


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.fixture
def env(tmp_path):
    """A project root, a transcripts dir laid out like ~/.claude/projects, a config."""
    root = tmp_path / "widgets"
    root.mkdir()
    tdir = tmp_path / "projects"
    cfg = _deep_merge(DEFAULTS, {
        "project_name": "widgets",
        "qdrant_url": "http://unused.invalid:6333",
        "sessions": {"transcripts_dir": str(tdir), "state_dir": str(tmp_path / "state")},
    })
    cfg["embed"]["embedding_workers"] = 1
    client = QdrantClient(":memory:")
    with patch("carta.embed.embed.get_embedding", side_effect=fake_embedding):
        yield {"root": root, "tdir": tdir, "cfg": cfg, "client": client, "tmp": tmp_path}


def _put(env, tr: Transcript) -> Path:
    d = env["tdir"] / transcript_slug(tr.cwd)
    return tr.write(d / f"{tr.session_id}.jsonl")


def _run(env, **kw):
    kw.setdefault("backfill", True)
    return sidx.run_index(env["cfg"], env["root"], client=env["client"], verbose=False, **kw)


def _points(env, flt=None):
    pts, _ = env["client"].scroll(sidx.sessions_collection(env["cfg"]), scroll_filter=flt,
                                  limit=1000, with_payload=True)
    return pts


def test_backfill_indexes_project_transcripts_only(env, tmp_path):
    _put(env, standard_session(str(env["root"])))
    other = tmp_path / "unrelated"
    _put(env, Transcript("sess-other", str(other)).prompt("nothing to do with widgets"))
    summary = _run(env)
    assert summary["indexed"] == 1
    pts = _points(env)
    assert pts and {p.payload["session_id"] for p in pts} == {"sess-alpha"}
    assert env["client"].collection_exists("widgets_sessions")


def test_payload_fields_and_full_record_on_chunk_zero(env):
    _put(env, standard_session(str(env["root"])))
    _run(env)
    pts = sorted(_points(env), key=lambda p: p.payload["chunk_index"])
    p0 = pts[0].payload
    for key in ("date", "repo", "worktree", "session_id", "start_ts", "end_ts", "branch",
                "text", "doc_type", "issues", "prs"):
        assert key in p0, key
    assert p0["doc_type"] == "session"
    assert p0["date"] == "2026-03-02"
    assert p0["repo"] == "widgets"
    assert p0["record"]["prompts"][0].startswith("Please make the widget counter")
    assert p0["record"]["final_text"].startswith("Done:")
    assert all("record" not in p.payload for p in pts[1:])


def test_unchanged_transcript_is_skipped(env):
    _put(env, standard_session(str(env["root"])))
    assert _run(env)["indexed"] == 1
    # Same size and mtime: the file is not even opened (no hash, no cwd probe).
    with patch("carta.sessions.index.index_record") as ir, \
         patch("carta.sessions.index._sha256") as sha, \
         patch("carta.sessions.index.first_cwd") as fc:
        s = _run(env)
    ir.assert_not_called()
    sha.assert_not_called()
    fc.assert_not_called()
    assert s["indexed"] == 0 and s["unchanged"] == 1


def test_touched_but_identical_transcript_is_skipped_by_sha(env):
    path = _put(env, standard_session(str(env["root"])))
    _run(env)
    os.utime(path, (time.time() + 100, time.time() + 100))
    with patch("carta.sessions.index.index_record") as ir:
        s = _run(env)
    ir.assert_not_called()
    assert s["unchanged"] == 1


def test_grown_transcript_is_reindexed_and_old_points_removed(env):
    tr = standard_session(str(env["root"]))
    path = _put(env, tr)
    _run(env)
    before = {p.id for p in _points(env)}

    more = Transcript("sess-alpha", str(env["root"]), branch="feat/widgets",
                      start_minutes=tr.t)
    more.prompt("one more thing: rename the counter to tally")
    more.say("Renamed it to tally.")
    tr.append_to(path, more.lines)

    s = _run(env)
    assert s["indexed"] == 1
    pts = _points(env)
    after = {p.id for p in pts}
    assert before.isdisjoint(after), "old generation must be gone"
    rec = next(p.payload["record"] for p in pts if p.payload["chunk_index"] == 0)
    assert rec["prompts"][-1] == "one more thing: rename the counter to tally"
    assert rec["final_text"] == "Renamed it to tally."


def test_force_reindexes_unchanged(env):
    _put(env, standard_session(str(env["root"])))
    _run(env)
    assert _run(env, force=True)["indexed"] == 1


def test_window_limits_non_backfill_runs(env):
    path = _put(env, standard_session(str(env["root"])))
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    assert _run(env, backfill=False)["indexed"] == 0
    assert _run(env, backfill=True)["indexed"] == 1


def test_single_session_file(env):
    p1 = _put(env, standard_session(str(env["root"]), session_id="s-one"))
    _put(env, standard_session(str(env["root"]), session_id="s-two"))
    s = _run(env, session_file=p1, backfill=False)
    assert s["indexed"] == 1
    assert {p.payload["session_id"] for p in _points(env)} == {"s-one"}


def test_session_file_outside_project_is_refused(env, tmp_path):
    p = _put(env, Transcript("x", str(tmp_path / "elsewhere")).prompt("hi"))
    s = _run(env, session_file=p, backfill=False)
    assert s["indexed"] == 0 and s["other_project"] == 1


def test_empty_session_recorded_and_not_reparsed(env):
    _put(env, Transcript("quiet", str(env["root"])).meta("only injected context"))
    s = _run(env)
    assert s["empty"] == 1 and s["indexed"] == 0
    with patch("carta.sessions.index.build_record") as br:
        s = _run(env)
    br.assert_not_called()
    assert s["unchanged"] == 1


def test_dry_run_writes_nothing(env):
    _put(env, standard_session(str(env["root"])))
    s = _run(env, dry_run=True)
    assert s["indexed"] == 1
    assert not env["client"].collection_exists("widgets_sessions")
    assert not (env["tmp"] / "state" / "widgets" / "index-state.json").exists()


def test_summarize_seam_fills_summary(env):
    _put(env, standard_session(str(env["root"])))
    _run(env, summarize=lambda rec: f"summary of {rec.session_id}")
    rec = next(p.payload["record"] for p in _points(env) if p.payload["chunk_index"] == 0)
    assert rec["summary"] == "summary of sess-alpha"


def test_summarize_failure_is_not_fatal(env):
    _put(env, standard_session(str(env["root"])))
    def boom(rec):
        raise RuntimeError("model down")
    assert _run(env, summarize=boom)["indexed"] == 1


def test_failed_upsert_leaves_state_untouched_for_retry(env):
    _put(env, standard_session(str(env["root"])))
    with patch("carta.sessions.index.upsert_chunks", return_value=0):
        with pytest.raises(sidx.SessionIndexError):
            _run(env)
    with patch("carta.sessions.index.index_record", wraps=sidx.index_record) as ir:
        assert _run(env)["indexed"] == 1
    assert ir.call_count == 1


# ---------------------------------------------------------------------------
# Filtering by date / worktree / repo
# ---------------------------------------------------------------------------

@pytest.fixture
def three_sessions(env):
    root = env["root"]
    wt = root / ".worktrees" / "feature-x"
    _put(env, standard_session(str(root), session_id="main-early", start_minutes=0))
    _put(env, standard_session(str(wt), session_id="wt-mid", branch="feat/x",
                               start_minutes=60 * 24 * 3))       # 3 days later
    _put(env, standard_session(str(root), session_id="main-late",
                               start_minutes=60 * 24 * 10))      # 10 days later
    _run(env)
    return env


def _ids(env, flt):
    return {p.payload["session_id"] for p in _points(env, flt)}


def test_filter_by_worktree(three_sessions):
    env = three_sessions
    assert _ids(env, sidx.session_filter(worktree="feature-x")) == {"wt-mid"}
    assert _ids(env, sidx.session_filter(worktree="widgets")) == {"main-early", "main-late"}


def test_filter_by_date_range(three_sessions):
    env = three_sessions
    from datetime import datetime, timezone
    since = int(datetime(2026, 3, 4, tzinfo=timezone.utc).timestamp())
    until = int(datetime(2026, 3, 10, tzinfo=timezone.utc).timestamp())
    assert _ids(env, sidx.session_filter(since_ts=since)) == {"wt-mid", "main-late"}
    assert _ids(env, sidx.session_filter(since_ts=since, until_ts=until)) == {"wt-mid"}
    assert _ids(env, sidx.session_filter(date="2026-03-12")) == {"main-late"}


def test_filter_combines_repo_and_worktree(three_sessions):
    env = three_sessions
    flt = sidx.session_filter(repo="widgets", worktree="feature-x")
    assert _ids(env, flt) == {"wt-mid"}
    assert _ids(env, sidx.session_filter(repo="nope")) == set()
    assert sidx.session_filter() is None


def test_filtered_dense_query(three_sessions):
    env = three_sessions
    coll = sidx.sessions_collection(env["cfg"])
    res = env["client"].query_points(
        coll, query=fake_embedding("thread-safe counter"),
        query_filter=sidx.session_filter(worktree="feature-x"), limit=10, with_payload=True)
    assert res.points and {p.payload["session_id"] for p in res.points} == {"wt-mid"}


# ---------------------------------------------------------------------------
# Hybrid collections (dense + bm25), as when fastembed is installed
# ---------------------------------------------------------------------------

def test_hybrid_schema_when_fastembed_present(env):
    _put(env, standard_session(str(env["root"])))
    with patch("carta.embed.embed._fastembed_available", return_value=True), \
         patch("carta.embed.sparse.embed_sparse_document", side_effect=fake_sparse):
        assert _run(env)["indexed"] == 1
    from carta.embed.embed import collection_is_hybrid
    coll = sidx.sessions_collection(env["cfg"])
    assert collection_is_hybrid(env["client"], coll)
    pts = _points(env)
    assert pts and all(p.payload["session_id"] == "sess-alpha" for p in pts)


def test_disabled_module_is_a_noop(env):
    env["cfg"]["modules"]["session_memory"] = False
    _put(env, standard_session(str(env["root"])))
    s = _run(env)
    assert s["disabled"] is True and s["indexed"] == 0
