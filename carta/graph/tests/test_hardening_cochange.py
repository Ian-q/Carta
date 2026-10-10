"""History as untrusted input: bounds placed before the memory, stored state never trusted."""
from __future__ import annotations

import hashlib
import io
import random
import sqlite3
import time
import zlib

import pytest

from carta.graph.build import build_graph, graph_status
from carta.graph.cochange import PairTable
from carta.graph.eval.holdout import HoldoutError, load_external_graph
from carta.graph.eval.replay import ReplayError, run_replay
from carta.graph.gitio import (
    GitError, _stream_tokens, first_parent_log, first_parent_rows, iter_log_entries,
)
from carta.graph.model import Edge
from carta.graph.query import _history_edges, format_impact, run_impact
from carta.graph.settings import CochangeSettings
from carta.graph.store import EdgeStore, store_path
from carta.graph.tests.conftest import requires_git

PLAIN = CochangeSettings(max_files=30, min_support=1, min_score=0.0, half_life=0)
SHA = "{:040x}".format
CFG = {"graph": {"cochange": {"max_files": 5, "half_life": 0}, "impact": {
    "kinds": ["mention", "cochange"]}}}


# -- the stream ------------------------------------------------------------------

class _Counting(io.BytesIO):
    def read(self, n=-1):
        self.calls = getattr(self, "calls", 0) + 1
        return super().read(n)


def test_a_record_with_no_end_is_given_up_while_it_is_read(monkeypatch):
    monkeypatch.setattr("carta.graph.gitio.MAX_GIT_TOKEN_BYTES", 1000)
    stream = _Counting(b"A" * 50_000_000)
    with pytest.raises(GitError, match="longer than"):
        next(_stream_tokens(stream))
    assert stream.calls <= 2                    # not after the other 50 MB went by
    assert list(_stream_tokens(io.BytesIO(b"a" * 1000 + b"\0b\0"))) == [b"a" * 1000, b"b"]
    with pytest.raises(GitError):
        list(_stream_tokens(io.BytesIO(b"a" * 1001 + b"\0")))


def _commit_tree_with_name(repo, name: bytes) -> None:
    """A commit whose tree holds one entry called ``name``, written as a loose object."""
    blob = repo.git("hash-object", "-w", "--stdin").strip() or None
    body = b"100644 " + name + b"\0" + bytes.fromhex(blob)
    raw = b"tree " + str(len(body)).encode() + b"\0" + body
    oid = hashlib.sha1(raw).hexdigest()
    path = repo.root / ".git" / "objects" / oid[:2] / oid[2:]
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(zlib.compress(raw))
    head = repo.git("rev-parse", "HEAD").strip()
    commit = repo.git("commit-tree", oid, "-p", head, "-m", "long name", date_index=9).strip()
    repo.git("update-ref", "HEAD", commit)


@requires_git
def test_a_tree_entry_with_an_enormous_name_fails_the_pass_and_keeps_the_store_stale(git_repo):
    git_repo.write("README.md", "x\n")
    git_repo.commit("first", 0)
    if len(git_repo.git("rev-parse", "HEAD").strip()) != 40:
        pytest.skip("the fixture writes a SHA-1 tree")
    _commit_tree_with_name(git_repo, b"A" * 300_000)        # 300 KB of name, ~0.3 KB on disk
    rows = first_parent_rows(git_repo.root)
    with pytest.raises(GitError, match="longer than"):
        list(iter_log_entries(git_repo.root, rows, max_changes=30))
    report = build_graph(git_repo.root, CFG)["cochange"]
    assert report["status"] == "failed" and len(report["detail"]) <= 300
    assert graph_status(git_repo.root, CFG)["stale"] is True


# -- the pair table ----------------------------------------------------------------

def _pairs_in(table) -> int:
    return sum(len(links) for links in table.adj.values()) // 2


def test_the_table_stops_starting_pairs_at_its_cap(monkeypatch):
    monkeypatch.setattr("carta.graph.cochange.MAX_COCHANGE_PAIRS", 10)
    table = PairTable(PLAIN)
    table.observe(SHA(1), [("M", "root")], has_parent=False)
    for i in range(2, 12):                      # each commit pairs four files never seen before
        table.observe(SHA(i), [("M", f"f{i}_{j}") for j in range(4)])
    assert table.counts()["pairs"] == _pairs_in(table) == 10
    assert table.capped == 10 * 6 - 10          # 6 per commit, 10 let in
    before = table.score("f2_0", "f2_1")
    table.observe(SHA(12), [("M", "f2_0"), ("M", "f2_1")])     # a pair it has goes on counting
    assert table.score("f2_0", "f2_1")[0] == before[0] + 1
    table.observe(SHA(13), [("D", "f2_0")])                    # a deletion makes room
    assert table.counts()["pairs"] == _pairs_in(table) == 7
    table.observe(SHA(14), [("M", "new_a"), ("M", "new_b")])
    assert table.counts()["pairs"] == 8


def test_a_full_table_survives_a_reload_and_an_overfull_one_is_not_loaded(monkeypatch):
    monkeypatch.setattr("carta.graph.cochange.MAX_COCHANGE_PAIRS", 3)
    table = PairTable(PLAIN)
    table.observe(SHA(1), [("M", "root")], has_parent=False)
    table.observe(SHA(2), [("M", p) for p in "abcd"])
    assert (table.counts()["pairs"], table.capped) == (3, 3)
    files = {path: table.file_meta(path) for path in table.files}
    again = PairTable.load(PLAIN, table.seq, table.totals(), files)
    assert (again.counts()["pairs"], again.capped) == (3, 3)
    monkeypatch.setattr("carta.graph.cochange.MAX_COCHANGE_PAIRS", 2)
    with pytest.raises(ValueError):
        PairTable.load(PLAIN, table.seq, table.totals(), files)


def test_the_running_pair_count_is_the_real_one_under_any_history():
    rng = random.Random(7)
    table = PairTable(CochangeSettings(max_files=6, min_support=1, min_score=0.0, half_life=0))
    names = [f"p{i}" for i in range(12)]
    for i in range(1, 400):
        changes = [(rng.choice("MMAD"), name) for name in rng.sample(names, rng.randint(1, 7))]
        blobs = {p: SHA(rng.randint(1, 4)) for s, p in changes if s != "D"}
        removed = {p: SHA(rng.randint(1, 4)) for s, p in changes if s == "D"}
        table.observe(SHA(i), changes, has_parent=i > 1, blobs=blobs, removed=removed)
        assert table.counts()["pairs"] == _pairs_in(table)
    assert "capped" not in table.totals()       # a table that was never full writes what it did


# -- stored state ------------------------------------------------------------------

def _history(repo):
    repo.write("README.md", "# project\n")
    repo.commit("root", 0)
    for step in (1, 2, 3):
        repo.write("src/a.py", f"a = {step}\n")
        repo.write("docs/a.md", f"about a, {step}\n")
        repo.commit(f"change {step}", step)
    return repo


def _tamper(root, statement, *params) -> None:
    con = sqlite3.connect(str(store_path(root)))
    con.execute(statement, params)
    con.commit()
    con.close()


@requires_git
def test_every_row_the_history_pass_writes_is_one_the_store_reads_back(git_repo):
    repo = _history(git_repo)
    first = build_graph(repo.root, CFG)
    assert first["by_kind"].get("cochange", 0) > 0
    before = store_path(repo.root).stat().st_ino
    second = build_graph(repo.root, CFG)
    # the second build found a store it believes: nothing re-extracted, nothing re-mined
    assert (second["extracted"], second["full"], second["digest"]) == (0, False, first["digest"])
    assert (second["cochange"]["new_commits"], second["cochange"]["written"]) == (0, 0)
    assert store_path(repo.root).stat().st_ino == before
    assert graph_status(repo.root, CFG)["stale"] is False
    with EdgeStore.open_readonly(store_path(repo.root)) as store:
        assert len(list(store.iter_edges(kinds=("cochange",)))) == first["by_kind"]["cochange"]


PLANTED_STATE = {
    "a weight of hundreds of digits": (
        "UPDATE sources SET meta = replace(meta, '\"w\":', '\"w\":1' || ? || ',\"x\":') "
        "WHERE extractor = 'cochange'", "0" * 400),
    "state nested past the parser": (
        "UPDATE sources SET meta = ? WHERE extractor = 'cochange'", "[" * 200_000),
    "totals nested past the parser": (
        "UPDATE meta SET value = ? WHERE key = 'cochange.totals'", "[" * 200_000),
    "a count in digits int() refuses": (
        "UPDATE meta SET value = ? WHERE key = 'cochange.seq'", "³"),
    "a count of thousands of digits": (
        "UPDATE meta SET value = ? WHERE key = 'cochange.seq'", "9" * 5000),
    "a cursor that is not a commit": (
        "UPDATE meta SET value = ? WHERE key = 'cochange.last_commit'", "\x1b[2Jnot-a-commit"),
}


@requires_git
@pytest.mark.parametrize("what", sorted(PLANTED_STATE))
def test_planted_pair_state_is_mined_again_not_trusted_and_not_a_crash(git_repo, what):
    repo = _history(git_repo)
    clean = build_graph(repo.root, CFG)["cochange"]
    _tamper(repo.root, *PLANTED_STATE[what])
    status = graph_status(repo.root, CFG)                   # must not raise
    assert "\x1b" not in str(status["cochange"])
    repo.write("src/a.py", "a = 9\n")
    repo.commit("one more", 4)                              # so the pass has to read the state
    report = build_graph(repo.root, CFG)["cochange"]
    assert report["status"] == "ok" and report["commits"] == clean["commits"] + 1
    assert report["digest"] == build_graph(repo.root, CFG, force=True)["cochange"]["digest"]


def test_history_rows_with_a_first_commit_that_is_not_one_are_kept_not_a_crash():
    def edge(meta):
        return Edge("cochange", "src/a.py", "docs/a.md", "doc", 0, meta=meta, src_type="file",
                    extractor="cochange")
    own = frozenset([SHA(5)])
    odd = [edge('{"first":[]}'), edge('{"first":{"a":1}}'), edge("[" * 200_000), edge("7")]
    assert _history_edges(odd, own) == odd
    assert _history_edges([edge('{"first":"%s"}' % SHA(5))], own) == []


# -- the holdout's external graph --------------------------------------------------

def test_external_graph_rows_are_bounded_before_the_work(tmp_path, monkeypatch):
    path = tmp_path / "g.tsv"
    path.write_text("./" * 400_000 + "a\tb\n" + "src/a.py\tsrc/b.py\n")
    started = time.perf_counter()
    graph, info = load_external_graph(path)
    assert time.perf_counter() - started < 5.0              # it was quadratic: minutes
    assert (info["edges"], info["rejected"]) == (1, 1)
    monkeypatch.setattr("carta.graph.eval.holdout.MAX_EXTERNAL_GRAPH_EDGES", 5)
    path.write_text("not an edge\n" * 10)                   # rejected rows count too
    with pytest.raises(HoldoutError, match="more than 5 rows"):
        load_external_graph(path)


def test_external_graph_weight_of_hundreds_of_digits_is_a_rejected_row(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('[["src/a.py", "src/b.py", 1%s], ["src/a.py", "src/c.py"]]' % ("0" * 400))
    graph, info = load_external_graph(path)
    assert (info["edges"], info["rejected"]) == (1, 1)


# -- the replay ----------------------------------------------------------------------

@requires_git
def test_the_replay_gives_up_a_history_it_cannot_hold(git_repo, monkeypatch):
    repo = _history(git_repo)
    assert len(first_parent_log(repo.root)) == 4
    with pytest.raises(GitError, match="more than 3 paths"):
        first_parent_log(repo.root, "HEAD", 3)
    monkeypatch.setattr("carta.graph.eval.replay.MAX_REPLAY_HISTORY_CHANGES", 3)
    with pytest.raises(ReplayError, match="more than 3 paths"):
        run_replay(repo.root, use_disk_cache=False, resamples=20)


@requires_git
def test_history_rows_are_printed_through_printable(git_repo):
    repo = _history(git_repo)
    result = run_impact(repo.root, CFG, paths=["src/a.py"])
    assert any(m["kind"] == "cochange" for r in result["results"] for m in r["matches"])
    for item in result["results"]:
        for match in item["matches"]:
            match["path"] = "src/\x1b[2Ja.py"
            for ev in match["evidence"]:
                ev["text"] = "\x1b]0;owned\x07" + ev["text"]
    result["unmatched_paths"] = ["\x1b[31mx"]
    text = format_impact(result)
    assert "\x1b" not in text and "\x07" not in text
