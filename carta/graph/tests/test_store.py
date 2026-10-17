"""EdgeStore: schema, per-source replacement, deterministic reads and the digest."""
from __future__ import annotations

import sqlite3

import pytest

from carta.graph.model import SCHEMA_VERSION, Edge, canonical_meta
from carta.graph.store import EdgeStore, StoreVersionError, store_path


def E(dst, line=1, kind="mention", src="docs/a.md", dst_type="file", **kw):
    return Edge(kind, src, dst, dst_type, line, **kw)


@pytest.fixture
def db(tmp_path):
    return store_path(tmp_path)


def all_edges(store, **kw):
    return list(store.iter_edges(**kw))


def test_store_path(tmp_path):
    assert store_path(tmp_path) == tmp_path / ".carta" / "graph" / "edges.db"


def test_creates_schema_dir_and_self_ignoring_gitignore(db):
    with EdgeStore(db):
        pass
    assert (db.parent / ".gitignore").read_text() == "*\n"
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        assert {"meta", "sources", "edges", "edges_dst", "edges_src", "edges_kind_type"} <= names
        edge_cols = [r[1] for r in con.execute("PRAGMA table_info(edges)")]
        assert edge_cols == ["id", "kind", "src", "src_type", "dst", "dst_type", "line", "key",
                             "evidence", "weight", "meta", "extractor", "extractor_version"]
        source_cols = [r[1] for r in con.execute("PRAGMA table_info(sources)")]
        assert source_cols == ["extractor", "path", "sha256", "extractor_version", "meta"]
    finally:
        con.close()
    assert not list(db.parent.glob("*-wal")) and not list(db.parent.glob("*-shm"))


def test_reopen_keeps_data(db):
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h1", 1, [E("src/a.py")])
    with EdgeStore(db) as s:
        assert [e.dst for e in all_edges(s)] == ["src/a.py"]


def test_replace_source_is_idempotent_and_replaces(db):
    with EdgeStore(db) as s:
        first = [E("src/a.py"), E("src/b.py", 2)]
        s.replace_source("mention", "docs/a.md", "h1", 1, first)
        s.replace_source("mention", "docs/a.md", "h1", 1, first)
        assert all_edges(s) == first
        s.replace_source("mention", "docs/a.md", "h2", 1, [E("src/c.py")])
        assert [e.dst for e in all_edges(s)] == ["src/c.py"]
        assert s.source_hashes("mention") == {"docs/a.md": "h2"}
        assert s.counts()["sources"] == 1


def test_replace_source_rejects_edges_of_another_source_or_extractor(db):
    with EdgeStore(db) as s:
        with pytest.raises(ValueError):
            s.replace_source("mention", "docs/a.md", "h", 1, [E("x.py", src="docs/other.md")])
        with pytest.raises(ValueError):
            s.replace_source("mention", "docs/a.md", "h", 1, [E("x.py", extractor="cochange")])
        assert all_edges(s) == [] and s.source_hashes("mention") == {}


def test_replace_source_only_touches_its_own_extractor(db):
    co = Edge("cochange", "docs/a.md", "src/a.py", "file", extractor="cochange", weight=0.4)
    with EdgeStore(db) as s:
        s.replace_source("cochange", "docs/a.md", "", 3, [co])
        s.replace_source("mention", "docs/a.md", "h1", 1, [E("src/a.py")])
        s.replace_source("mention", "docs/a.md", "h2", 1, [])
        assert all_edges(s) == [co]
        assert s.source_hashes("cochange") == {"docs/a.md": ""}
        assert s.source_hashes("mention") == {"docs/a.md": "h2"}


def test_remove_source_and_clear_extractor(db):
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1, [E("src/a.py")])
        s.replace_source("mention", "docs/b.md", "h", 1, [E("src/b.py", src="docs/b.md")])
        s.replace_source("symbol", "docs/a.md", "h", 1,
                         [E("src/a.py", kind="symbol", extractor="symbol", key="f")])
        s.remove_source("mention", "docs/a.md")
        assert [(e.kind, e.src) for e in all_edges(s)] == [
            ("mention", "docs/b.md"), ("symbol", "docs/a.md")]
        s.remove_source("mention", "docs/never.md")
        s.clear_extractor("mention")
        assert [(e.kind, e.src) for e in all_edges(s)] == [("symbol", "docs/a.md")]
        assert s.source_hashes("mention") == {}


def test_duplicate_identity_within_one_write_keeps_one_row(db):
    """Documented behaviour: the last edge with a given identity wins; no crash."""
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1,
                         [E("src/a.py", evidence="first"), E("src/a.py", evidence="second")])
        (edge,) = all_edges(s)
        assert edge.evidence == "second"


def test_iter_edges_order_and_filters(db):
    edges = [E("src/b.py", 2), E("src/a.py", 9), E("src/a.py", 1),
             E("tok", kind="ident", dst_type="token"), E("src/", dst_type="dir")]
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1, edges)
        s.replace_source("mention", "docs/0.md", "h", 1, [E("src/z.py", src="docs/0.md")])
        got = all_edges(s)
        assert got == sorted(got, key=lambda e: (e.kind, e.src, e.dst, e.line, e.key))
        assert [e.kind for e in got] == ["ident"] + ["mention"] * 5
        assert [e.dst for e in all_edges(s, kinds=("ident",))] == ["tok"]
        assert [e.dst for e in all_edges(s, dst_types=("dir", "token"))] == ["tok", "src/"]
        assert [e.dst for e in all_edges(s, kinds=("mention",), dst_types=("dir",))] == ["src/"]
        assert all_edges(s, kinds=()) == [] and all_edges(s, extractors=("cochange",)) == []
        assert len(all_edges(s, extractors=("mention",))) == 6
        assert s.source_edges("mention", "docs/0.md") == [E("src/z.py", src="docs/0.md")]


def test_roundtrip_preserves_every_field(db):
    edge = Edge(kind="mention", src="docs/é.md", dst="src/naïve file.py", dst_type="file", line=41,
                key="k", evidence="Voir `src/naïve file.py` … 日本語", weight=0.5,
                meta=canonical_meta({"ctx": "fence", "raw": "x"}), src_type="doc",
                extractor="mention", extractor_version=7)
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/é.md", "h", 7, [edge])
    with EdgeStore(db) as s:
        assert all_edges(s) == [edge]


def test_schema_accepts_later_kinds_without_migration(db):
    """The contract #130 and #131 rely on: their rows fit schema version 1."""
    co = Edge("cochange", "src/a.py", "docs/a.md", "doc", 0, weight=0.37, src_type="file",
              evidence="changed together in 9 commits", extractor="cochange",
              meta=canonical_meta({"support": 9, "first": "abc", "last": "def"}))
    co_back = Edge("cochange", "docs/a.md", "src/a.py", "file", 0, weight=0.37, src_type="doc",
                   extractor="cochange", meta=canonical_meta({"support": 9}))
    # two identifiers on ONE doc line, both defined in ONE file: two rows, told apart by key
    sym1 = Edge("symbol", "docs/a.md", "src/frame.py", "file", 41, key="parse_frame",
                extractor="symbol", meta=canonical_meta({"name": "parse_frame", "def_line": 10}))
    sym2 = Edge("symbol", "docs/a.md", "src/frame.py", "file", 41, key="check_crc",
                extractor="symbol", meta=canonical_meta({"name": "check_crc", "def_line": 30}))
    rel = Edge("related", "docs/a.md", "docs/b.md", "doc", 0, extractor="related",
               evidence="defines the ids this page uses")
    with EdgeStore(db) as s:
        s.replace_source("cochange", "src/a.py", "", 1, [co], meta=canonical_meta({"commits": 12}))
        s.replace_source("cochange", "docs/a.md", "", 1, [co_back])
        s.replace_source("symbol", "docs/a.md", "h", 1, [sym1, sym2])
        s.replace_source("related", "docs/a.md", "h", 1, [rel])
        s.set_meta("cochange.last_commit", "def")
        assert set(all_edges(s)) == {co, co_back, sym1, sym2, rel}
        assert len(all_edges(s, kinds=("symbol",))) == 2
        assert s.source_meta("cochange") == {"docs/a.md": "{}", "src/a.py": '{"commits":12}'}
        s.set_source_meta("cochange", "src/a.py", canonical_meta({"commits": 13}))
        assert s.source_meta("cochange")["src/a.py"] == '{"commits":13}'
        assert all_edges(s, kinds=("cochange",)) == [co_back, co]     # edges untouched
        assert s.get_meta("cochange.last_commit") == "def"


def _fill(store, order):
    edges = {"docs/a.md": [E("src/a.py"), E("src/b.py", 2, weight=0.5)],
             "docs/b.md": [E("src/a.py", src="docs/b.md", evidence="é")]}
    for src in order:
        store.replace_source("mention", src, "h-" + src, 1, edges[src])


def test_digest_ignores_insert_order_and_row_ids(tmp_path):
    one, two = EdgeStore(store_path(tmp_path / "1")), EdgeStore(store_path(tmp_path / "2"))
    with one, two:
        _fill(one, ["docs/a.md", "docs/b.md"])
        _fill(two, ["docs/b.md", "docs/a.md"])
        two.replace_source("mention", "docs/c.md", "h", 1, [E("x.py", src="docs/c.md")])
        two.remove_source("mention", "docs/c.md")              # burns row ids
        assert one.edges_digest() == two.edges_digest()
        assert len(one.edges_digest()) == 64


def test_digest_changes_with_any_field(db):
    base = E("src/a.py", evidence="e")
    variants = [
        E("src/a.py", evidence="other"), E("src/a.py", 2, evidence="e"),
        E("src/a.py", evidence="e", weight=0.5), E("src/a.py", evidence="e", key="k"),
        E("src/a.py", evidence="e", meta='{"a":1}'), E("src/a.py", evidence="e", dst_type="doc"),
        E("src/b.py", evidence="e"), E("src/a.py", evidence="e", extractor_version=99),
        E("src/a.py", evidence="e", kind="ident"),
    ]
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1, [base])
        seen = {s.edges_digest()}
        for variant in variants:
            s.replace_source("mention", "docs/a.md", "h", 1, [variant])
            seen.add(s.edges_digest())
        s.replace_source("mention", "docs/a.md", "h2", 1, [base])      # only the source hash
        seen.add(s.edges_digest())
        assert len(seen) == len(variants) + 2


def test_digest_can_be_scoped_to_an_extractor(db):
    with EdgeStore(db) as s:
        _fill(s, ["docs/a.md", "docs/b.md"])
        mention_only, everything = s.edges_digest(("mention",)), s.edges_digest()
        s.replace_source("cochange", "src/a.py", "", 1, [
            Edge("cochange", "src/a.py", "docs/a.md", "doc", src_type="file", extractor="cochange")])
        assert s.edges_digest(("mention",)) == mention_only
        assert s.edges_digest() != everything
        assert s.edges_digest(("cochange",)) != s.edges_digest(("mention",))


def test_counts(db):
    with EdgeStore(db) as s:
        assert s.counts() == {"sources": 0, "edges": 0, "by_kind": {}, "by_dst_type": {}}
        s.replace_source("mention", "docs/a.md", "h", 1, [
            E("src/a.py"), E("src/", dst_type="dir"), E("tok", kind="ident", dst_type="token")])
        assert s.counts() == {"sources": 1, "edges": 3, "by_kind": {"ident": 1, "mention": 2},
                              "by_dst_type": {"dir": 1, "file": 1, "token": 1}}
        assert list(s.counts()["by_kind"]) == ["ident", "mention"]


def _set_version(db, version):
    con = sqlite3.connect(str(db))
    con.execute(f"PRAGMA user_version = {version}")
    con.commit()
    con.close()


def test_older_schema_is_rebuilt_empty(db):
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1, [E("src/a.py")])
    _set_version(db, 0)
    with EdgeStore(db) as s:
        assert all_edges(s) == [] and s.source_hashes("mention") == {}
        assert s.schema_version == SCHEMA_VERSION


def test_newer_schema_is_left_alone_and_reported(db):
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1, [E("src/a.py")])
    _set_version(db, SCHEMA_VERSION + 98)
    before = db.read_bytes()
    with pytest.raises(StoreVersionError, match="newer"):
        EdgeStore(db)
    assert db.read_bytes() == before


def test_corrupt_file_is_replaced(db):
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database, not even close" * 50)
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1, [E("src/a.py")])
        assert len(all_edges(s)) == 1


def test_transaction_rolls_back_on_exception(db):
    with EdgeStore(db) as s:
        s.replace_source("mention", "docs/a.md", "h", 1, [E("src/a.py")])
        with pytest.raises(RuntimeError):
            with s.transaction():
                s.replace_source("mention", "docs/b.md", "h", 1, [E("x.py", src="docs/b.md")])
                s.remove_source("mention", "docs/a.md")
                s.set_meta("k", "v")
                raise RuntimeError("boom")
        assert [e.src for e in all_edges(s)] == ["docs/a.md"]
        assert s.get_meta("k") is None
        with s.transaction():                       # the store is still usable, and nests
            with s.transaction():
                s.set_meta("k", "v")
        assert s.get_meta("k") == "v"


def test_meta_get_set(db):
    with EdgeStore(db) as s:
        assert s.get_meta("mention.paths_sha") is None
        s.set_meta("mention.paths_sha", "abc")
        s.set_meta("mention.paths_sha", "def")
        assert s.get_meta("mention.paths_sha") == "def"


def test_readonly_open_does_not_create(db):
    assert EdgeStore.open_existing(db) is None
    assert not db.exists() and not db.parent.exists()
    with EdgeStore(db) as s:
        s.set_meta("k", "v")
    existing = EdgeStore.open_existing(db)
    assert existing is not None
    with existing:
        assert existing.get_meta("k") == "v"
