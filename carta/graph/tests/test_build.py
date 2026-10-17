"""build_graph: discovery, incremental rebuilds by content hash, and reproducibility."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from carta.graph.build import build_graph, content_sha256, graph_status, list_repo_paths
from carta.graph.model import MENTION_EXTRACTOR_VERSION
from carta.graph.store import EdgeStore, store_path
from carta.graph.tests.conftest import requires_git

CFG: dict = {}


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "proj"
    write(root, "README.md", "Start with `docs/parser.md` and `src/parser/frame.py`.\n")
    write(root, "docs/parser.md", "Rules live in `src/parser/frame.py`, see `parse_frame()`.\n"
                                  "Old code was `src/parser/legacy.py`.\n")
    write(root, "docs/store.md", "Everything under `src/store/`.\n")
    write(root, "src/parser/frame.py", "x = 1\n")
    write(root, "src/store/db.py", "y = 2\n")
    return root


def stored(root, **kw):
    with EdgeStore(store_path(root)) as s:
        return [(e.src, e.dst, e.dst_type) for e in s.iter_edges(**kw)]


def digest(root):
    with EdgeStore(store_path(root)) as s:
        return s.edges_digest(("mention",))


# -- hashing and discovery ----------------------------------------------------

def test_content_sha256_matches_embed_lifecycle_for_text(tmp_path):
    """Parity with the embedding arm's hash. The package may not import it; a test may."""
    pytest.importorskip("qdrant_client")
    from carta.embed.lifecycle import compute_file_hash
    lf, crlf = b"# T\n\nbody\n", b"# T\r\n\r\nbody\r\n"
    assert content_sha256(lf) == content_sha256(crlf)
    path = tmp_path / "a.md"
    path.write_bytes(crlf)
    assert content_sha256(crlf) == compute_file_hash(path)
    assert content_sha256(b"") != content_sha256(b"\n")


def test_list_repo_paths_walk_fallback(tree):
    write(tree, ".git/HEAD", "ref")                       # a fake, not a work tree git accepts
    shutil.rmtree(tree / ".git")
    write(tree, "node_modules/pkg/README.md", "x")
    write(tree, "pkg/.venv/lib/a.md", "x")
    write(tree, ".carta/config.yaml", "x")
    write(tree, ".carta/graph/edges.db", "x")
    assert list_repo_paths(tree) == [
        "README.md", "docs/parser.md", "docs/store.md", "src/parser/frame.py", "src/store/db.py"]


@requires_git
def test_list_repo_paths_git(git_repo):
    git_repo.write(".gitignore", "ignored/\n*.log\n")
    git_repo.write("docs/a.md", "a")
    git_repo.write("src/gone.py", "g")
    git_repo.write("src/with space é.py", "s")
    git_repo.commit("first")
    git_repo.write("untracked.md", "u")
    git_repo.write("ignored/x.md", "i")
    git_repo.write("run.log", "l")
    git_repo.write(".carta/config.yaml", "c")
    git_repo.rm("src/gone.py")                            # tracked, deleted in the work tree
    assert list_repo_paths(git_repo.root) == [
        ".gitignore", "docs/a.md", "src/with space é.py", "untracked.md"]


@requires_git
def test_list_repo_paths_relative_to_a_subdirectory_root(git_repo):
    git_repo.write("top.md", "t")
    git_repo.write("sub/proj/docs/a.md", "a")
    git_repo.write("sub/proj/src/a.py", "a")
    git_repo.commit("first")
    assert list_repo_paths(git_repo.root / "sub" / "proj") == ["docs/a.md", "src/a.py"]


@requires_git
def test_nested_repository_is_not_a_path(git_repo, make_git_repo):
    git_repo.write("top.md", "t")
    git_repo.commit("first")
    inner = make_git_repo("repo/sub/inner")
    inner.write("x.md", "x")
    inner.commit("inner")
    assert list_repo_paths(git_repo.root) == ["top.md"]


# -- builds --------------------------------------------------------------------

def test_first_build_extracts_every_doc_and_reports_counts(tree):
    report = build_graph(tree, CFG)
    assert report["enabled"] is True and report["full"] is True
    assert (report["docs"], report["extracted"], report["unchanged"], report["removed"]) == (3, 3, 0, 0)
    assert report["by_kind"] == {"ident": 1, "mention": 5}
    assert report["edges"] == 6 and len(report["digest"]) == 64
    assert stored(tree, kinds=("mention",)) == [
        ("README.md", "docs/parser.md", "doc"), ("README.md", "src/parser/frame.py", "file"),
        ("docs/parser.md", "src/parser/frame.py", "file"),
        ("docs/parser.md", "src/parser/legacy.py", "missing"),
        ("docs/store.md", "src/store/", "dir")]
    assert (tree / ".carta" / "graph" / ".gitignore").read_text() == "*\n"


def test_second_build_is_a_noop(tree):
    first = build_graph(tree, CFG)
    second = build_graph(tree, CFG)
    assert (second["extracted"], second["unchanged"], second["full"]) == (0, 3, False)
    assert second["digest"] == first["digest"] and second["written"] == 0


def test_editing_one_doc_re_extracts_only_it(tree):
    build_graph(tree, CFG)
    write(tree, "docs/store.md", "Now `src/store/db.py`.\n")
    report = build_graph(tree, CFG)
    assert (report["extracted"], report["unchanged"], report["full"]) == (1, 2, False)
    assert ("docs/store.md", "src/store/db.py", "file") in stored(tree)
    assert ("docs/store.md", "src/store/", "dir") not in stored(tree)


def test_crlf_rewrite_of_a_doc_is_not_a_change(tree):
    build_graph(tree, CFG)
    text = (tree / "docs/store.md").read_bytes().replace(b"\n", b"\r\n")
    (tree / "docs/store.md").write_bytes(text)
    assert build_graph(tree, CFG)["extracted"] == 0


def test_adding_a_source_file_triggers_full_pass_and_resolves_a_missing_edge(tree):
    build_graph(tree, CFG)
    write(tree, "src/parser/legacy.py", "z = 3\n")
    report = build_graph(tree, CFG)
    assert report["full"] is True and report["extracted"] == 3
    assert report["written"] == 1                      # only the doc whose edges changed
    assert ("docs/parser.md", "src/parser/legacy.py", "file") in stored(tree)


def test_deleting_a_source_file_turns_edge_into_missing(tree):
    build_graph(tree, CFG)
    (tree / "src/parser/frame.py").unlink()
    build_graph(tree, CFG)
    assert ("docs/parser.md", "src/parser/frame.py", "missing") in stored(tree)
    assert ("README.md", "src/parser/frame.py", "missing") in stored(tree)


def test_deleting_a_doc_removes_its_source_and_edges(tree):
    build_graph(tree, CFG)
    (tree / "docs/store.md").unlink()
    report = build_graph(tree, CFG)
    assert report["removed"] == 1 and report["docs"] == 2
    assert not [e for e in stored(tree) if e[0] == "docs/store.md"]
    with EdgeStore(store_path(tree)) as s:
        assert sorted(s.source_hashes("mention")) == ["README.md", "docs/parser.md"]


def test_excluded_docs_are_not_sources_but_can_be_targets(tree):
    write(tree, "drafts/idea.md", "`src/store/db.py`\n")
    write(tree, "docs/store.md", "See `drafts/idea.md`.\n")
    cfg = {"excluded_paths": ["drafts/"]}
    assert build_graph(tree, cfg)["docs"] == 3
    edges = stored(tree)
    assert not [e for e in edges if e[0] == "drafts/idea.md"]
    assert ("docs/store.md", "drafts/idea.md", "file") in edges
    # un-excluding is a settings change: full pass, and the draft becomes a source
    report = build_graph(tree, {"excluded_paths": []})
    assert report["full"] is True and report["docs"] == 4
    assert ("docs/store.md", "drafts/idea.md", "doc") in stored(tree)
    assert build_graph(tree, cfg)["removed"] == 1


def test_always_excluded_dirs_are_never_sources(tree):
    write(tree, "node_modules/pkg/README.md", "`src/store/db.py`\n")
    assert build_graph(tree, CFG)["docs"] == 3


def test_settings_change_triggers_full_pass(tree):
    build_graph(tree, CFG)
    cfg = {"graph": {"patterns": [{"name": "rule", "regex": r"\bRules\b"}]}}
    report = build_graph(tree, cfg)
    assert report["full"] is True and report["by_kind"].get("pattern") == 1
    assert build_graph(tree, cfg)["full"] is False
    assert build_graph(tree, {"graph": {"impact": {"top": 3}}})["full"] is True   # pattern gone
    assert build_graph(tree, {"graph": {"impact": {"top": 5}}})["full"] is False  # not an input


def test_force_rebuilds_everything(tree):
    first = build_graph(tree, CFG)
    report = build_graph(tree, CFG, force=True)
    assert report["full"] is True and report["extracted"] == 3 and report["written"] == 3
    assert report["digest"] == first["digest"]


def test_extractor_version_bump_triggers_full_rewrite(tree, monkeypatch):
    build_graph(tree, CFG)
    with EdgeStore(store_path(tree)) as s:
        s.set_meta("mention.extractor_version", str(MENTION_EXTRACTOR_VERSION - 1))
    assert build_graph(tree, CFG)["full"] is True


EDITS = {
    "edit_doc": lambda r: write(r, "docs/parser.md", "Now only `src/store/db.py` and `EdgeStore`.\n"),
    "add_src": lambda r: write(r, "src/parser/legacy.py", "z\n"),
    "del_src": lambda r: (r / "src/store/db.py").unlink(),
    "add_doc": lambda r: write(r, "docs/new.md", "[p](parser.md) and `src/parser/*.py`\n"),
    "del_doc": lambda r: (r / "docs/store.md").unlink(),
}


@pytest.mark.parametrize("order", [
    ["edit_doc", "add_src", "del_src", "add_doc", "del_doc"],
    ["del_doc", "add_doc", "del_src", "add_src", "edit_doc"],
    ["add_src", "del_doc", "edit_doc", "del_src", "add_doc"],
])
def test_incremental_equals_fresh(tree, order):
    build_graph(tree, CFG)
    for step in order:
        EDITS[step](tree)
        incremental = build_graph(tree, CFG)["digest"]
    store_path(tree).unlink()
    fresh = build_graph(tree, CFG)
    assert fresh["digest"] == incremental == digest(tree)


def test_same_tree_in_two_places_gives_the_same_digest(tree, tmp_path):
    other = tmp_path / "elsewhere" / "copy"
    shutil.copytree(tree, other)
    assert build_graph(tree, CFG)["digest"] == build_graph(other, CFG)["digest"]


def test_delete_db_and_rebuild_reproduces_digest(tree):
    first = build_graph(tree, CFG)["digest"]
    shutil.rmtree(tree / ".carta" / "graph")
    assert build_graph(tree, CFG)["digest"] == first


def test_build_does_not_change_the_path_set(tree):
    """The store lives in .carta/, which is never in the path set, so a build
    cannot make the next one a full pass."""
    write(tree, ".carta/config.yaml", "project_name: demo\n")
    build_graph(tree, CFG)
    write(tree, ".carta/scan-results.json", "{}")
    assert build_graph(tree, CFG)["full"] is False


def test_build_when_graph_disabled_returns_disabled_report_and_writes_nothing(tree):
    report = build_graph(tree, {"graph": {"enabled": False}})
    assert report["enabled"] is False and report["edges"] == 0 and report["docs"] == 0
    assert not (tree / ".carta").exists()


def test_unreadable_or_binary_doc_is_skipped_not_fatal(tree):
    (tree / "docs" / "bad.md").write_bytes(b"\xff\xfe `src/store/db.py` \xc3\x28\n")
    (tree / "docs" / "blob.md").write_bytes(b"PK\x00\x03 `src/store/db.py`\n")
    report = build_graph(tree, CFG)
    assert report["docs"] == 5
    edges = stored(tree)
    assert ("docs/bad.md", "src/store/db.py", "file") in edges      # decoded with replacement
    assert not [e for e in edges if e[0] == "docs/blob.md"]         # NUL byte: binary, no edges
    assert build_graph(tree, CFG)["extracted"] == 0


def test_stored_rows_hold_no_absolute_paths_or_timestamps(tree):
    build_graph(tree, CFG)
    with EdgeStore(store_path(tree)) as s:
        dump = repr(list(s.iter_edges())) + repr(s.source_hashes("mention"))
        dump += repr([s.get_meta(k) for k in (
            "mention.settings_sha", "mention.paths_sha", "mention.extractor_version")])
    assert str(tree) not in dump and str(tree.parent) not in dump
    assert str(time.gmtime().tm_year) not in dump


# -- status ----------------------------------------------------------------------

def test_graph_status_reports_stale_reasons(tree):
    status = graph_status(tree, CFG)
    assert status["exists"] is False and status["stale"] is True
    assert status["stale_reasons"] == ["no_store"]
    assert not (tree / ".carta").exists()                 # status never creates anything

    built = build_graph(tree, CFG)
    status = graph_status(tree, CFG)
    assert status["exists"] is True and status["stale"] is False and status["stale_reasons"] == []
    assert status["digest"] == built["digest"] and status["docs"] == 3 and status["edges"] == 6
    assert status["by_kind"] == {"ident": 1, "mention": 5}
    assert status["by_dst_type"] == {"dir": 1, "doc": 1, "file": 2, "missing": 1, "token": 1}
    assert (status["schema_version"], status["extractor_version"]) == (1, MENTION_EXTRACTOR_VERSION)

    write(tree, "docs/store.md", "changed\n")
    assert graph_status(tree, CFG)["stale_reasons"] == ["docs_changed"]
    write(tree, "src/new.py", "n\n")
    assert graph_status(tree, CFG)["stale_reasons"] == ["paths_changed", "docs_changed"]
    cfg = {"graph": {"doc_globs": ["docs/**/*.md"]}}
    assert "settings_changed" in graph_status(tree, cfg)["stale_reasons"]
    assert graph_status(tree, CFG)["digest"] == built["digest"]      # status wrote nothing


def test_graph_status_keys(tree):
    keys = {"enabled", "exists", "schema_version", "extractor_version", "docs", "edges",
            "by_kind", "by_dst_type", "digest", "stale", "stale_reasons"}
    assert set(graph_status(tree, CFG)) == keys
    build_graph(tree, CFG)
    assert set(graph_status(tree, CFG)) == keys


# -- budget ------------------------------------------------------------------------

TIMED = bool(os.environ.get("CARTA_TIMING_TESTS"))


def test_build_budget(tmp_path):
    """500 docs of about 16 kB with about 110 code spans each: a full build in under 10 s.

    The wall-clock half only runs with ``CARTA_TIMING_TESTS=1``: on a loaded or
    small machine a time limit fails for reasons that have nothing to do with
    the code. Without it the test builds a fifth of the corpus and checks the
    work done, which is what a regression in the extractor would change.
    """
    n_docs = 500 if TIMED else 100
    root = tmp_path / "big"
    sources = [f"src/pkg{p}/mod{m}.py" for p in range(20) for m in range(25)]
    for rel in sources:
        write(root, rel, "x\n")
    filler = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod. " * 2
    for d in range(n_docs):
        lines = []
        for i in range(110):
            target = sources[(d * 7 + i * 13) % len(sources)]
            lines.append(f"{filler}See `{target}` and `handle_case_{i}()`.")
        write(root, f"docs/area{d % 10}/doc{d}.md", "\n".join(lines) + "\n")
    start = time.perf_counter()
    report = build_graph(root, CFG)
    elapsed = time.perf_counter() - start
    assert report["docs"] == n_docs and report["by_kind"]["mention"] == n_docs * 110
    assert report["extracted"] == n_docs
    again = build_graph(root, CFG)
    assert (again["extracted"], again["unchanged"], again["written"]) == (0, n_docs, 0)
    if TIMED:
        assert elapsed < 10.0, f"full build took {elapsed:.1f}s"


# -- review round 2: status and --no-build never write -------------------------------

@pytest.mark.parametrize("damage", ["garbage", "truncated", "foreign"])
def test_status_reports_an_unreadable_store_and_leaves_it_alone(tree, damage):
    build_graph(tree, CFG)
    db = store_path(tree)
    if damage == "garbage":
        db.write_bytes(b"garbage not sqlite")
    elif damage == "truncated":
        db.write_bytes(db.read_bytes()[:1500])
    else:
        db.unlink()
        import sqlite3
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE something_else (x)")
        con.commit()
        con.close()
    before = db.read_bytes()
    listing = sorted(p.name for p in db.parent.iterdir())

    status = graph_status(tree, CFG)
    assert status["exists"] is True and status["stale"] is True
    assert status["stale_reasons"] == ["unreadable"]
    assert (status["docs"], status["edges"], status["digest"]) == (0, 0, "")
    from carta.graph.query import ImpactError, run_impact
    with pytest.raises(ImpactError, match="carta graph build"):
        run_impact(tree, CFG, paths=["src/parser/frame.py"], build=False)
    assert db.read_bytes() == before
    assert sorted(p.name for p in db.parent.iterdir()) == listing

    healed = build_graph(tree, CFG)                       # a build still repairs it
    assert healed["edges"] == 6 and graph_status(tree, CFG)["stale"] is False
