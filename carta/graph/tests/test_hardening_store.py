"""A store a repository ships: nothing read back from ``edges.db`` is trusted."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from carta.graph.build import build_graph, graph_status
from carta.graph.model import Edge
from carta.graph.query import ImpactError, format_impact, run_impact
from carta.graph.store import (
    EdgeStore, StoreUnreadableError, StoreUnsafeError, store_path,
)

CFG: dict = {}


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "proj"
    write(root, "README.md", "Start with `src/app.py`.\n")
    write(root, "src/app.py", "x = 1\n")
    return root


@pytest.fixture
def db(tmp_path):
    path = store_path(tmp_path)
    with EdgeStore(path) as s:
        s.replace_source("mention", "docs/a.md", "h", 3,
                         [Edge("mention", "docs/a.md", "src/a.py", "file", 1, evidence="ok")])
        s.set_meta("mention.paths_sha", "abc")
    return path


def tamper(path: Path, *statements) -> None:
    """Write to the file the way anything but Carta would: no constraint is in the way."""
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA ignore_check_constraints = ON")
    for statement in statements:
        if isinstance(statement, tuple):
            con.execute(*statement)
        else:
            con.execute(statement)
    con.commit()
    con.close()


PLANTED = {
    "an unknown kind": "UPDATE edges SET kind = 'exec'",
    "an unknown target type": "UPDATE edges SET dst_type = 'url'",
    "an unknown source type": "UPDATE edges SET src_type = 'cmd'",
    "a source outside the repository": "UPDATE edges SET src = '../../home/someone/.netrc'",
    "an absolute source": "UPDATE edges SET src = '/etc/hosts'",
    "a target outside the repository": "UPDATE edges SET dst = 'src/../../outside.py'",
    "an infinite weight": "UPDATE edges SET weight = 9e999",
    "a negative weight": "UPDATE edges SET weight = -5.0",
    "a weight that is text": "UPDATE edges SET weight = 'heavy'",
    "evidence that is bytes": "UPDATE edges SET evidence = X'1B5B324A'",
    "evidence with an escape sequence": ("UPDATE edges SET evidence = ?", ("\x1b[2Jfake",)),
    "evidence with a bidi override": ("UPDATE edges SET evidence = ?", ("a‮b",)),
    "oversized evidence": ("UPDATE edges SET evidence = ?", ("x" * 5000,)),
    "a line that is text": "UPDATE edges SET line = 'first'",
    "a negative line": "UPDATE edges SET line = -1",
    "meta that is not an object": "UPDATE edges SET meta = '[]'",
    "meta that is not JSON": "UPDATE edges SET meta = '{'",
    "meta nested past the parser": ("UPDATE edges SET meta = ?", ("[" * 60_000,)),
    "an extractor that is not a name": ("UPDATE edges SET extractor = ?", ("\x1b[31m",)),
    "a source row outside the repository": "UPDATE sources SET path = '../x.md'",
    "a source hash that is bytes": "UPDATE sources SET sha256 = X'00'",
    "a meta value that is bytes": "UPDATE meta SET value = X'00FF'",
    "a meta key that is not a name": ("UPDATE meta SET key = ?", ("\x1b]0;title\x07",)),
}


@pytest.mark.parametrize("what", sorted(PLANTED))
def test_a_planted_row_makes_the_file_not_a_store(db, what):
    tamper(db, PLANTED[what])
    with pytest.raises(StoreUnreadableError):
        EdgeStore.open_readonly(db)
    # a build may replace it: what it opens is empty, and usable
    with EdgeStore(db) as s:
        assert list(s.iter_edges()) == [] and s.source_hashes("mention") == {}
        assert s.get_meta("mention.paths_sha") is None
    with EdgeStore.open_readonly(db) as s:
        assert s.counts()["edges"] == 0


@pytest.mark.parametrize("extra", [
    "CREATE TRIGGER t AFTER INSERT ON edges BEGIN DELETE FROM sources; END",
    "CREATE VIEW v AS SELECT * FROM edges",
    "CREATE TABLE other (x)",
    "CREATE INDEX edges_x ON edges (lower(evidence))",
])
def test_a_schema_with_anything_added_is_not_a_store(db, extra):
    tamper(db, extra)
    with pytest.raises(StoreUnreadableError):
        EdgeStore.open_readonly(db)
    with EdgeStore(db) as s:
        assert list(s.iter_edges()) == []
    con = sqlite3.connect(str(db))
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    assert names == {"meta", "sources", "edges", "edges_dst", "edges_src", "edges_kind_type",
                     "sqlite_autoindex_edges_1"}


def test_a_table_swapped_for_a_view_is_not_a_store(db):
    tamper(db, "ALTER TABLE meta RENAME TO meta_real",
           "CREATE VIEW meta AS SELECT key, value FROM meta_real")
    with pytest.raises(StoreUnreadableError):
        EdgeStore.open_readonly(db)


def test_a_store_carta_wrote_reopens_as_it_was(db):
    with EdgeStore.open_readonly(db) as s:
        before = s.edges_digest()
    with EdgeStore(db) as s:
        assert s.edges_digest() == before and len(list(s.iter_edges())) == 1
        assert s.get_meta("mention.paths_sha") == "abc"


def test_an_oversized_store_is_refused_and_left_alone(db, monkeypatch):
    monkeypatch.setattr("carta.graph.store.MAX_STORE_BYTES", 1024)
    before = db.read_bytes()
    assert len(before) > 1024
    for opener in (EdgeStore, EdgeStore.open_readonly, EdgeStore.open_existing):
        with pytest.raises(StoreUnsafeError, match="larger than"):
            opener(db)
    assert db.read_bytes() == before


def test_a_row_that_goes_bad_after_the_open_is_an_error_not_data(db):
    with EdgeStore.open_readonly(db) as s:
        tamper(db, "UPDATE edges SET weight = 9e999", "UPDATE meta SET value = X'00'",
               "UPDATE sources SET path = '/abs'")
        for read in (lambda: list(s.iter_edges()), lambda: s.get_meta("mention.paths_sha"),
                     lambda: s.source_hashes("mention"), lambda: s.source_meta("mention"),
                     lambda: s.source_edges("mention", "docs/a.md"), s.edges_digest):
            with pytest.raises(StoreUnreadableError):
                read()


def _forge(root: Path) -> None:
    """Keep the doc's hash, so an incremental build has no reason to re-read it."""
    tamper(store_path(root),
           ("UPDATE edges SET weight = 9e999, evidence = ?", ("\x1b]0;owned\x07\x1b[2JLGTM",)),
           ("INSERT INTO edges (kind, src, dst, dst_type, line, evidence, weight, extractor, "
            "extractor_version) VALUES ('mention', '../../outside/notes.md', 'src/app.py', "
            "'file', 1, 'x', 1.0, 'mention', 3)"))


def test_a_build_does_not_keep_forged_rows(tree):
    clean = build_graph(tree, CFG)
    _forge(tree)
    report = build_graph(tree, CFG)
    assert (report["extracted"], report["edges"], report["digest"]) == (1, 1, clean["digest"])
    result = run_impact(tree, CFG, paths=["src/app.py"])
    assert [r["doc"] for r in result["results"]] == ["README.md"]
    assert math.isfinite(result["results"][0]["score"])
    text = format_impact(result) + json.dumps(result)
    assert "\x1b" not in text and "owned" not in text and "outside" not in text


def test_forged_rows_are_never_ranked_or_printed_without_a_build(tree):
    build_graph(tree, CFG)
    _forge(tree)
    before = store_path(tree).read_bytes()
    with pytest.raises(ImpactError) as raised:
        run_impact(tree, CFG, paths=["src/app.py"], build=False)
    assert "carta graph build" in str(raised.value)
    status = graph_status(tree, CFG)
    assert status["stale"] is True and status["stale_reasons"] == ["unreadable"]
    assert store_path(tree).read_bytes() == before          # a reader never repairs
