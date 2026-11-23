"""`carta recall` core: search, grouping by session, filters, passages, eval scaffold."""
from pathlib import Path
from unittest.mock import patch

import pytest
from qdrant_client import QdrantClient

from carta.sessions import recall as rc
from carta.sessions.tests.corpus import (
    bow_embedding, bow_sparse, build_index, make_cfg, utc_tz,
)
from carta.sessions.tests.synth import T0

EXAMPLE_SET = Path(rc.__file__).resolve().parent.parent / "eval" / "datasets" / \
    "sessions-example.yaml"


@pytest.fixture
def env(tmp_path, monkeypatch):
    with utc_tz(monkeypatch):
        root = tmp_path / "widgets"
        root.mkdir()
        cfg = make_cfg(root, tmp_path)
        yield {"root": root, "cfg": cfg, "tmp": tmp_path}


@pytest.fixture
def indexed(env):
    env["client"] = build_index(env["cfg"], env["root"])
    with patch("carta.embed.embed.get_embedding", side_effect=bow_embedding):
        yield env


def _recall(env, q, **kw):
    return rc.recall(q, env["cfg"], client=env["client"], **kw)


# ---------------------------------------------------------------------------
# parse_since / best_passage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,seconds", [("90m", 5400), ("36h", 36 * 3600),
                                           ("14d", 14 * 86400), ("2w", 14 * 86400),
                                           (" 3D ", 3 * 86400)])
def test_parse_since_spans(value, seconds):
    assert rc.parse_since(value, now=1_000_000_000) == 1_000_000_000 - seconds


def test_parse_since_date_and_blank(env):
    assert rc.parse_since("2026-03-04") == int(T0.timestamp()) + 2 * 86400 - 9 * 3600
    assert rc.parse_since(None) is None and rc.parse_since("  ") is None


@pytest.mark.parametrize("bad", ["yesterday", "14", "d14", "2026-13-01"])
def test_parse_since_rejects_garbage(bad):
    with pytest.raises(ValueError):
        rc.parse_since(bad)


def test_best_passage_picks_the_matching_line():
    text = "# Title\n\nrepo widgets\n\n1. unrelated words here\n2. add retries to the exporter"
    assert rc.best_passage(text, "exporter retries").startswith("2. add retries")


def test_best_passage_is_trimmed():
    text = "match " + "word " * 200
    out = rc.best_passage(text, "match", max_chars=50)
    assert len(out) <= 50 and out.endswith("…")
    assert rc.best_passage("", "x") == ""


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------

def test_recall_finds_the_session_and_reports_its_record(indexed):
    res = _recall(indexed, "exporter timing out, retries with backoff")
    top = res[0]
    assert top["session_id"] == "sess-exporter"
    assert top["rank"] == 1
    assert top["worktree"] == "exporter" and top["branch"] == "fix/exporter-retry"
    assert top["date"] == "2026-03-04"
    assert top["first_prompt"].startswith("The CSV exporter keeps timing out")
    assert top["issues"] == [61]
    assert top["passages"] and any("exporter" in p["text"].lower() for p in top["passages"])
    assert top["file_path"].startswith("session:sess-exporter | 2026-03-04 | exporter")


def test_recall_returns_each_session_once(indexed):
    res = _recall(indexed, "widget counter thread-safe", limit=10)
    ids = [r["session_id"] for r in res]
    assert len(ids) == len(set(ids))
    assert ids[0] == "sess-counter"
    assert res[0]["prs"] == [57]


def test_recall_limit(indexed):
    assert len(_recall(indexed, "the", limit=2)) <= 2


def test_recall_worktree_filter(indexed):
    res = _recall(indexed, "exporter retries", worktree="theme")
    assert [r["session_id"] for r in res] == ["sess-theme"]


def test_recall_branch_filter(indexed):
    res = _recall(indexed, "anything", branch="main")
    assert [r["session_id"] for r in res] == ["sess-readme"]


def test_recall_since_filter(indexed):
    since = int(T0.timestamp()) + 3 * 86400 - 60     # just before sess-theme started
    res = _recall(indexed, "exporter retries", since_ts=since)
    assert {r["session_id"] for r in res} == {"sess-theme", "sess-readme"}


def test_recall_passages_zero(indexed):
    assert _recall(indexed, "dark mode", passages=0)[0]["passages"] == []


def test_recall_empty_query_rejected(indexed):
    with pytest.raises(ValueError):
        _recall(indexed, "   ")


def test_recall_missing_collection(env):
    with pytest.raises(rc.RecallUnavailable):
        rc.recall("x", env["cfg"], client=QdrantClient(":memory:"))


def test_recall_hybrid_collection(env):
    env["cfg"]["search"]["hybrid"]["enabled"] = True
    client = build_index(env["cfg"], env["root"], hybrid=True)
    from carta.embed.embed import collection_is_hybrid
    assert collection_is_hybrid(client, "widgets_sessions")
    with patch("carta.embed.embed.get_embedding", side_effect=bow_embedding), \
         patch("carta.embed.pipeline.embed_sparse_query", side_effect=bow_sparse):
        res = rc.recall("dark mode palette settings page", env["cfg"], client=client)
    assert res[0]["session_id"] == "sess-theme"


def test_recall_keeps_newest_generation_record(indexed):
    """A re-indexed session (it grew) reports its newest record."""
    from carta.sessions.tests.corpus import sessions, write_all
    from carta.sessions import index as sidx
    tr = [t for t in sessions(indexed["root"]) if t.session_id == "sess-theme"][0]
    tr.prompt("also fix the contrast of the dark mode buttons")
    write_all(indexed["cfg"], [tr])
    sidx.run_index(indexed["cfg"], indexed["root"], client=indexed["client"], backfill=True,
                   verbose=False)
    res = _recall(indexed, "contrast of the buttons")
    assert res[0]["session_id"] == "sess-theme"


def test_format_results(indexed):
    text = rc.format_results(_recall(indexed, "exporter retries", limit=1))
    assert text.startswith("[1] 2026-03-04 ")
    assert "widgets/exporter (fix/exporter-retry)" in text
    assert "refs: #61" in text and "    > " in text
    assert rc.format_results([]) == ""


# ---------------------------------------------------------------------------
# Eval scaffold
# ---------------------------------------------------------------------------

def test_example_eval_set_scores_on_the_fictional_corpus(indexed, capsys):
    from carta.eval.harness import load_eval_set, load_eval_target
    assert load_eval_target(EXAMPLE_SET) == "sessions"
    assert len(load_eval_set(EXAMPLE_SET)) >= 5
    out_json = indexed["tmp"] / "m.json"
    m = rc.run_sessions_eval(EXAMPLE_SET, indexed["cfg"], k=3, json_path=str(out_json),
                             client=indexed["client"])
    assert m["target"] == "sessions"
    # The toy bag-of-words embedder cannot match "export" to "exporter", so the short
    # query may miss; the descriptive ones must land first.
    ranks = {row["q"]: row["first_hit_rank"] for row in m["per_query"]}
    assert ranks["the time we made the counter safe to use from several threads"] == 1
    assert ranks["when did we add retries to the exporter that kept timing out"] == 1
    assert ranks["fix for the dark mode colours on the settings page"] == 1
    assert m["recall_at_k"] >= 0.6
    assert m["reject_violations"] == 0
    out = capsys.readouterr().out
    assert "target=sessions" in out and out_json.exists()


def test_eval_target_validation(tmp_path):
    from carta.eval.harness import load_eval_target
    p = tmp_path / "e.yaml"
    p.write_text("queries: []\n")
    assert load_eval_target(p) == "docs"
    p.write_text("target: notes\nqueries: []\n")
    with pytest.raises(ValueError):
        load_eval_target(p)


def test_eval_miss_is_scored_as_miss(indexed, tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text("target: sessions\nqueries:\n  - q: dark mode\n"
                 "    expect: ['session:does-not-exist']\n")
    m = rc.run_sessions_eval(p, indexed["cfg"], k=3, client=indexed["client"])
    assert m["recall_at_k"] == 0.0
