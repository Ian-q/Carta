"""The replay's incremental mention state and its blob cache (no git needed)."""
from __future__ import annotations

import random
import sqlite3

from carta.graph.eval.cache import ReplayCache, default_cache_path
from carta.graph.eval.incremental import MentionState
from carta.graph.markdown import scan_text
from carta.graph.mentions import resolve_candidates
from carta.graph.model import Candidate
from carta.graph.paths import PathIndex
from carta.graph.query import ImpactIndex, rank_impact
from carta.graph.settings import DEFAULT_SETTINGS

DIRS = ["src", "src/parser", "src/store", "lib", "tools/gen", "docs", "docs/design"]
NAMES = ["frame.py", "checksum.py", "db.py", "util.py", "README.md", "notes.md", "Makefile"]
MENTIONS = [
    "`{d}/{n}`", "`{n}`", "`{d}/`", "[link]({n})", "[up](../{n})", "`./{n}`",
    "`{d}/*.py`", "`src/**/{n}`", "`src/{{parser,store}}/{n}`", "`parser/{n}`",
    "`gone/{n}`", "`{d}/missing.py`", "`parse_frame()`", "`{d}/{n}:12`", "`../{d}/{n}`",
]


def _doc_text(rng: random.Random) -> str:
    lines = []
    for _ in range(rng.randint(1, 8)):
        template = rng.choice(MENTIONS)
        lines.append("See " + template.format(d=rng.choice(DIRS), n=rng.choice(NAMES)) + ".")
    return "\n".join(lines) + "\n"


def _scratch(doc, cands, paths):
    return resolve_candidates(doc, cands, PathIndex(paths), DEFAULT_SETTINGS)


def test_incremental_state_equals_from_scratch_on_random_histories():
    for seed in range(6):
        rng = random.Random(seed)
        all_paths = sorted({f"{d}/{n}" for d in DIRS for n in NAMES} | set(NAMES))
        paths = set(rng.sample(all_paths, 12))
        docs = {}
        state = MentionState(DEFAULT_SETTINGS)
        index = PathIndex(paths)
        for doc in ("README.md", "docs/a.md", "docs/design/b.md", "lib/c.md", "tools/gen/d.md"):
            docs[doc] = scan_text(_doc_text(rng))
            state.set_doc(doc, docs[doc])
        state.refresh(index)
        total = 0
        for _step in range(60):
            changed = []
            for _ in range(rng.randint(0, 3)):
                path = rng.choice(all_paths)
                if path in paths:
                    paths.discard(path)
                    index.remove(path)
                else:
                    paths.add(path)
                    index.add(path)
                changed.append(path)
            state.paths_changed(changed)
            roll = rng.random()
            if roll < 0.25:
                doc = rng.choice(sorted(docs))
                docs[doc] = scan_text(_doc_text(rng))
                state.set_doc(doc, docs[doc])
            elif roll < 0.3 and len(docs) > 2:
                doc = rng.choice(sorted(docs))
                del docs[doc]
                state.remove_doc(doc)
            if rng.random() < 0.7:              # sometimes several commits pass unrefreshed
                total += state.refresh(index)
                for doc, cands in docs.items():
                    assert state.doc_edges(doc) == _scratch(doc, cands, paths), (seed, doc)
                expected = sorted(e for d, c in docs.items() for e in _scratch(d, c, paths))
                assert state.edges() == expected
        # the point of the exercise: far fewer resolutions than docs x steps
        assert total < 60 * len(docs)


def test_non_incremental_state_resolves_every_doc_every_time():
    state = MentionState(DEFAULT_SETTINGS, incremental=False)
    index = PathIndex(["src/a.py"])
    for doc in ("a.md", "b.md", "c.md"):
        state.set_doc(doc, scan_text("`src/a.py`\n"))
    assert state.refresh(index) == 3
    assert state.refresh(index) == 0            # nothing changed at all
    index.add("zzz/unrelated.txt")
    state.paths_changed(["zzz/unrelated.txt"])
    assert state.refresh(index) == 3
    lean = MentionState(DEFAULT_SETTINGS)
    for doc in ("a.md", "b.md", "c.md"):
        lean.set_doc(doc, scan_text("`src/a.py`\n"))
    lean.refresh(index)
    lean.paths_changed(["zzz/other.txt"])
    assert lean.refresh(index) == 0
    lean.paths_changed(["lib/a.py"])            # shares the basename: must be re-resolved
    assert lean.refresh(index) == 3


def test_edges_for_ranks_exactly_like_the_full_edge_set():
    rng = random.Random(11)
    all_paths = sorted({f"{d}/{n}" for d in DIRS for n in NAMES})
    index = PathIndex(all_paths)
    state = MentionState(DEFAULT_SETTINGS)
    docs = [f"docs/d{i}.md" for i in range(25)]
    for doc in docs:
        state.set_doc(doc, scan_text(_doc_text(rng)))
    state.refresh(index)
    for _ in range(40):
        query = rng.sample(all_paths, rng.randint(1, 6))
        full = rank_impact(ImpactIndex(state.edges(), len(docs)), query, top=None)
        lean = rank_impact(ImpactIndex(state.edges_for(query), len(docs)), query, top=None)
        assert lean == full
        assert len(state.edges_for(query)) <= len(state.edges())


def test_excluding_docs_equals_filtering_the_full_ranking():
    rng = random.Random(3)
    all_paths = sorted({f"{d}/{n}" for d in DIRS for n in NAMES})
    state = MentionState(DEFAULT_SETTINGS)
    docs = [f"docs/d{i}.md" for i in range(20)]
    for doc in docs:
        state.set_doc(doc, scan_text(_doc_text(rng)))
    state.refresh(PathIndex(all_paths))
    index = ImpactIndex(state.edges(), len(docs))
    query = all_paths[:8]
    full = [r["doc"] for r in rank_impact(index, query, top=None)]
    assert len(full) > 4
    hubs = set(full[:2])
    cut = [r["doc"] for r in rank_impact(index, query, top=3, exclude_docs=hubs)]
    assert cut == [d for d in full if d not in hubs][:3]


# -- cache -------------------------------------------------------------------

CANDS = [Candidate("src/a.py", "code", 3, "See `src/a.py`."),
         Candidate("ID-12", "pattern", 4, "ID-12 é", single=False, pattern="ticket")]


def test_cache_memory_roundtrip_and_keys():
    cache = ReplayCache(None)
    assert cache.get_scan("b1", "fp") is None
    cache.put_scan("b1", "fp", CANDS)
    assert cache.get_scan("b1", "fp") == CANDS
    assert cache.get_scan("b1", "other-fp") is None
    cache.put_tokens("b1", 1, {"a.py": 2})
    assert cache.get_tokens("b1", 1) == {"a.py": 2}
    assert cache.get_tokens("b1", 2) is None
    cache.close()


def test_cache_persists_on_disk(tmp_path):
    path = tmp_path / "deep" / "cache.db"
    with ReplayCache(path) as cache:
        assert cache.on_disk
        cache.put_scan("b1", "fp", CANDS)
        cache.put_tokens("b1", 1, {"é.md": 1})
    with ReplayCache(path) as again:
        assert again.get_scan("b1", "fp") == CANDS
        assert again.get_tokens("b1", 1) == {"é.md": 1}
        assert again.get_scan("b1", "fp2") is None


def test_cache_falls_back_to_memory_when_the_file_is_unusable(tmp_path):
    path = tmp_path / "cache.db"
    path.write_bytes(b"this is not a database, " * 100)
    with ReplayCache(path) as cache:
        assert not cache.on_disk
        cache.put_scan("b1", "fp", CANDS)
        assert cache.get_scan("b1", "fp") == CANDS
    blocked = tmp_path / "file"
    blocked.write_text("x")
    with ReplayCache(blocked / "sub" / "cache.db") as cache:
        assert not cache.on_disk


def test_cache_ignores_a_corrupt_row(tmp_path):
    path = tmp_path / "cache.db"
    with ReplayCache(path) as cache:
        cache.put_scan("b1", "fp", CANDS)
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE scan SET payload = ?", (b"garbage",))
    conn.commit()
    conn.close()
    with ReplayCache(path) as cache:
        assert cache.get_scan("b1", "fp") is None


def test_default_cache_path_is_under_carta_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CARTA_HOME", str(tmp_path / "home"))
    one = default_cache_path(tmp_path / "repo-a")
    two = default_cache_path(tmp_path / "repo-b")
    assert one != two and one == default_cache_path(tmp_path / "repo-a")
    assert one.parent == tmp_path / "home" / "cache" / "graph-replay"
    assert one.suffix == ".db" and len(one.stem) == 16
