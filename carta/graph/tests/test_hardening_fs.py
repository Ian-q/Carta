"""Symbolic links, special files and failed reads: never followed, never mistaken for fresh."""
from __future__ import annotations

import json
import os
import sqlite3
import zlib
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from carta.graph import limits
from carta.graph.build import build_graph, graph_status, list_repo_paths, read_doc
from carta.graph.eval.cache import ReplayCache
from carta.graph.eval.replay import main as replay_main
from carta.graph.query import ImpactError, format_impact, normalise_query_paths, run_impact
from carta.graph.safeio import (
    SkippedFile, create_exclusive, read_regular_file, write_text_replacing,
)
from carta.graph.scan import missing_path_findings
from carta.graph.store import EdgeStore, StoreUnreadableError, StoreUnsafeError, store_path
from carta.graph.tests.conftest import requires_git

needs_symlinks = pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                                    reason="needs POSIX symbolic links")
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
not_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                              reason="root can read a file whatever its mode")

SECRET = "API_TOKEN=hunter2-not-in-repo"
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
def outside(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    write(out, "secret.md", f"deploy with `src/app.py` {SECRET}\n")
    return out


def all_edges(root):
    with EdgeStore(store_path(root)) as s:
        return list(s.iter_edges())


# -- the read helper ---------------------------------------------------------------

@needs_symlinks
def test_read_regular_file_refuses_links_in_any_component(tree, outside):
    os.symlink(outside / "secret.md", tree / "link.md")
    os.symlink(outside, tree / "linkdir")
    os.symlink(tree / "src", tree / "inside")              # even one that stays in the repo
    os.symlink(tree / "nowhere", tree / "dangling.md")
    for rel in ("link.md", "linkdir/secret.md", "inside/app.py", "dangling.md"):
        with pytest.raises(SkippedFile) as raised:
            read_regular_file(tree, rel, 1000)
        assert raised.value.reason == "symlink", rel
    assert read_regular_file(tree, "src/app.py", 1000) == b"x = 1\n"


def test_read_regular_file_other_rules(tree):
    for rel, reason in (("src", "not_regular"), ("gone.md", "missing"), ("src/app.py/x", "missing"),
                        ("../proj/README.md", "unsafe_path"), ("/etc/hosts", "unsafe_path"),
                        ("src//app.py", "unsafe_path"), ("", "unsafe_path")):
        with pytest.raises(SkippedFile) as raised:
            read_regular_file(tree, rel, 1000)
        assert raised.value.reason == reason, rel
    with pytest.raises(SkippedFile) as raised:
        read_regular_file(tree, "src/app.py", 5)
    assert raised.value.reason == "too_large"
    assert read_regular_file(tree, "src/app.py", 6) == b"x = 1\n"


@needs_symlinks
def test_the_root_itself_may_be_a_link(tree, tmp_path):
    os.symlink(tree, tmp_path / "alias")
    assert read_regular_file(tmp_path / "alias", "src/app.py", 1000) == b"x = 1\n"


@needs_fifo
def test_read_regular_file_does_not_block_on_a_fifo(tree):
    os.mkfifo(tree / "pipe.md")
    with pytest.raises(SkippedFile) as raised:
        read_regular_file(tree, "pipe.md", 1000)
    assert raised.value.reason == "not_regular"


# -- docs --------------------------------------------------------------------------

@needs_symlinks
def test_a_symlinked_doc_is_never_read(tree, outside):
    os.makedirs(tree / "docs")
    os.symlink(outside / "secret.md", tree / "docs" / "notes.md")
    assert "docs/notes.md" in list_repo_paths(tree)           # still a name docs can mention
    report = build_graph(tree, CFG)
    assert (report["docs"], report["skipped"], report["unreadable"]) == (1, 1, 0)
    assert all(SECRET not in e.evidence and e.src != "docs/notes.md" for e in all_edges(tree))
    result = run_impact(tree, CFG, paths=["src/app.py"])
    assert SECRET not in json.dumps(result) and SECRET not in format_impact(result)
    assert [r["doc"] for r in result["results"]] == ["README.md"]
    assert graph_status(tree, CFG)["stale"] is False            # a rule, not a failure
    assert read_doc(tree, "docs/notes.md") == (None, "skipped")


@needs_symlinks
@requires_git
def test_a_tracked_symlinked_doc_is_never_read(git_repo, outside):
    git_repo.write("README.md", "Start with `src/app.py`.\n")
    git_repo.write("src/app.py", "x = 1\n")
    os.symlink(outside / "secret.md", git_repo.root / "notes.md")
    git_repo.commit("first", 0)
    assert build_graph(git_repo.root, CFG)["skipped"] == 1
    # the deletion path re-reads every doc in memory: it must not follow the link either
    result = run_impact(git_repo.root, CFG, paths=["src/gone.py", "src/app.py"])
    assert SECRET not in json.dumps(result)
    assert missing_path_findings(git_repo.root, CFG) == []


@needs_fifo
def test_a_fifo_named_like_a_doc_does_not_hang_the_build(tree):
    os.makedirs(tree / "docs")
    os.mkfifo(tree / "docs" / "x.md")
    report = build_graph(tree, CFG)
    assert (report["docs"], report["skipped"]) == (1, 1)
    assert graph_status(tree, CFG)["stale"] is False
    run_impact(tree, CFG, paths=["src/gone.py"])


def test_a_doc_over_the_size_bound_is_skipped(tree, monkeypatch):
    write(tree, "docs/big.md", "`src/app.py`\n" * 20)
    monkeypatch.setattr("carta.graph.build.MAX_DOC_BYTES", 100)
    report = build_graph(tree, CFG)
    assert (report["docs"], report["skipped"], report["unreadable"]) == (1, 1, 0)
    assert not [e for e in all_edges(tree) if e.src == "docs/big.md"]


@not_root
def test_a_doc_that_fails_to_read_keeps_its_rows_and_the_store_is_stale(tree):
    doc = write(tree, "docs/b.md", "Also `src/app.py`.\n")
    first = build_graph(tree, CFG)
    assert (first["docs"], first["edges"]) == (2, 2)
    doc.chmod(0)
    try:
        second = build_graph(tree, CFG)
        assert (second["skipped"], second["unreadable"], second["removed"]) == (1, 1, 0)
        assert second["edges"] == 2 and second["digest"] == first["digest"]
        status = graph_status(tree, CFG)
        assert status["stale"] is True and status["stale_reasons"] == ["docs_unreadable"]
    finally:
        doc.chmod(0o644)
    # readable again, but the last build did not read it: still not fresh
    assert graph_status(tree, CFG)["stale_reasons"] == ["docs_unreadable"]
    third = build_graph(tree, CFG)
    assert (third["unreadable"], third["edges"]) == (0, 2)
    assert graph_status(tree, CFG)["stale"] is False


def test_a_hostile_glob_in_a_doc_does_not_abort_the_build_or_the_scan(tree):
    write(tree, "NOTES.md", "glob `src/[z-a]*.py` and `src/app.py`\n")
    report = build_graph(tree, CFG)
    assert report["docs"] == 2 and report["edges"] == 2
    assert graph_status(tree, CFG)["stale"] is False
    assert run_impact(tree, CFG, paths=["src/app.py"])["matched_docs"] == 2
    assert missing_path_findings(tree, CFG) == []


def test_impact_output_holds_no_escape_sequences(tree):
    write(tree, "NOTES.md", "`src/app.py` \x1b]0;pwned\x07\x1b[2J\x1b[31mFAKE OUTPUT\n")
    text = format_impact(run_impact(tree, CFG, paths=["src/app.py"]))
    assert "\x1b" not in text and "\x07" not in text and "FAKE OUTPUT" in text


@requires_git
def test_the_not_a_range_hint_is_not_an_existence_oracle(git_repo, tmp_path):
    git_repo.write("README.md", "x\n")
    git_repo.commit("first", 0)
    (tmp_path / "outside-file").write_text("x")
    with pytest.raises(ImpactError) as inside:
        run_impact(git_repo.root, CFG, diff="README.md")
    assert "is a path, not a range" in str(inside.value)
    for probe in ("../outside-file", str(tmp_path / "outside-file"), "x/../../outside-file"):
        with pytest.raises(ImpactError) as raised:
            run_impact(git_repo.root, CFG, diff=probe)
        assert "is a path" not in str(raised.value), probe


@needs_symlinks
@requires_git
def test_the_not_a_range_hint_does_not_look_through_a_link(git_repo, tmp_path):
    """A repository can ship a link to any directory; the hint must not probe it."""
    beyond = tmp_path / "beyond"
    beyond.mkdir()
    (beyond / "there.txt").write_text("x")
    git_repo.write("README.md", "x\n")
    os.symlink(beyond, git_repo.root / "link")
    git_repo.commit("first", 0)
    messages = set()
    for probe in ("link/there.txt", "link/not-there.txt"):
        with pytest.raises(ImpactError) as raised:
            run_impact(git_repo.root, CFG, diff=probe)
        messages.add(str(raised.value).replace(probe, "PROBE"))
    assert len(messages) == 1 and "is a path" not in messages.pop()


@requires_git
def test_a_bad_range_error_does_not_pass_on_what_git_printed(git_repo):
    git_repo.write("README.md", "x\n")
    git_repo.commit("first", 0)
    for bad in ("nope..HEAD", "HEAD:README.md..HEAD", "HEAD..@{u}", "HEAD:../x..HEAD"):
        with pytest.raises(ImpactError) as raised:
            run_impact(git_repo.root, CFG, diff=bad)
        text = str(raised.value)
        assert "fatal" not in text and "usage" not in text and "\n" not in text, bad
        assert str(git_repo.root) not in text and str(git_repo.root.resolve()) not in text, bad


@needs_symlinks
def test_query_paths_do_not_say_what_a_link_points_at(tree, tmp_path):
    beyond = tmp_path / "beyond"
    (beyond / "sub").mkdir(parents=True)
    (beyond / "a-file").write_text("x")
    os.symlink(beyond / "sub", tree / "to-dir")
    os.symlink(beyond / "a-file", tree / "to-file")
    os.symlink(beyond / "nothing", tree / "to-nothing")
    os.symlink(beyond, tree / "link")
    typed = ["to-dir", "to-file", "to-nothing", "link/sub", "link/a-file", "link/nothing"]
    paths, skipped = normalise_query_paths(tree, tree, typed)
    assert (paths, skipped) == (sorted(typed), [])          # none gains a "/", none is dropped
    # the same question from a directory below the root: the reading never depends
    # on whether something exists beyond the link
    os.symlink(beyond, tree / "src" / "link")
    paths, _ = normalise_query_paths(tree, tree / "src", ["link/a-file", "link/nothing"])
    assert paths == ["link/a-file", "link/nothing"]
    # a real directory still reads as one
    assert normalise_query_paths(tree, tree, ["src"]) == (["src/"], [])


def test_impact_output_writes_out_control_characters_in_names(tree):
    name = "docs/\x1b[2J\x1b]0;owned\x07\u202egpj.md"
    write(tree, name, "see `src/app.py` and `lib/\x1b[31mgone.py`\n")
    result = run_impact(tree, CFG, paths=["src/app.py", "lib/\x1b[31mgone.py", "\x1b[1mnone.py"])
    assert name in [r["doc"] for r in result["results"]]    # the data keeps the real name
    text = format_impact(result)
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text
    assert "\\x1b[2J" in text and "\\u202e" in text


@needs_symlinks
def test_impact_errors_name_the_store_by_its_place_in_the_repository(tree, tmp_path):
    build_graph(tree, CFG)
    db = store_path(tree)
    db.write_bytes(b"not a database" * 100)
    with pytest.raises(ImpactError) as unreadable:
        run_impact(tree, CFG, paths=["src/app.py"], build=False)
    db.unlink()
    os.symlink(tmp_path / "elsewhere.db", db)
    with pytest.raises(ImpactError) as unsafe:
        run_impact(tree, CFG, paths=["src/app.py"])
    for raised in (unreadable, unsafe):
        assert ".carta/graph/edges.db" in str(raised.value)
        assert str(tmp_path) not in str(raised.value)


# -- the store ---------------------------------------------------------------------

@needs_symlinks
def test_the_gitignore_is_never_written_through_a_link(tree, tmp_path):
    target = tmp_path / "created-by-carta"
    os.makedirs(tree / ".carta" / "graph")
    os.symlink(target, tree / ".carta" / "graph" / ".gitignore")       # dangling
    build_graph(tree, CFG)
    assert not target.exists()


@needs_symlinks
@pytest.mark.parametrize("linked", [".carta", ".carta/graph"])
def test_a_symlinked_store_directory_is_refused(tree, tmp_path, linked):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tree / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(elsewhere, link)
    with pytest.raises(StoreUnsafeError):
        build_graph(tree, CFG)
    assert list(elsewhere.rglob("*")) == []
    with pytest.raises(ImpactError) as raised:
        run_impact(tree, CFG, paths=["src/app.py"])
    assert "symbolic link" in str(raised.value)


@needs_symlinks
@pytest.mark.parametrize("suffix", ["", "-journal", "-wal"])
def test_a_symlinked_store_file_is_refused(tree, tmp_path, suffix):
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"")
    os.makedirs(tree / ".carta" / "graph")
    os.symlink(victim, str(store_path(tree)) + suffix)
    with pytest.raises(StoreUnsafeError):
        build_graph(tree, CFG)
    assert victim.read_bytes() == b""
    status = graph_status(tree, CFG)
    if suffix == "":
        assert (status["stale"], status["stale_reasons"]) == (True, ["unsafe_store"])
        with pytest.raises(StoreUnreadableError):
            EdgeStore.open_readonly(store_path(tree))
        with pytest.raises(StoreUnsafeError):
            EdgeStore.open_existing(store_path(tree))
    assert victim.read_bytes() == b""


def test_a_store_outside_the_carta_layout_still_opens(tmp_path):
    with EdgeStore(tmp_path / "plain.db") as store:
        assert store.counts()["edges"] == 0
    assert (tmp_path / ".gitignore").read_text() == "*\n"


# -- the write helpers -------------------------------------------------------------

@needs_symlinks
def test_write_text_replacing_replaces_a_link_and_leaves_its_target(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS")
    link = tmp_path / "out" / "report.json"
    link.parent.mkdir()
    os.symlink(victim, link)
    write_text_replacing(link, "new\n")
    assert victim.read_text() == "PRECIOUS"
    assert not link.is_symlink() and link.read_text() == "new\n"
    assert sorted(p.name for p in link.parent.iterdir()) == ["report.json"]      # no temp left
    write_text_replacing(link, "again\n")
    assert link.read_text() == "again\n"
    with pytest.raises(OSError):
        write_text_replacing(tmp_path / "out", "x")                 # a directory
    assert create_exclusive(tmp_path / "fresh", "a") is True
    assert create_exclusive(tmp_path / "fresh", "b") is False
    assert (tmp_path / "fresh").read_text() == "a"


# -- the replay command ------------------------------------------------------------

@pytest.fixture
def small_history(git_repo):
    git_repo.write("README.md", "See `src/app.py`.\n")
    git_repo.write("src/app.py", "x = 1\n")
    git_repo.commit("first", 0)
    git_repo.write("README.md", "See `src/app.py`, updated.\n")
    git_repo.write("src/app.py", "x = 2\n")
    git_repo.commit("second", 1)
    return git_repo


ARGS = ["--no-cache", "--resamples", "20"]


@needs_symlinks
@requires_git
def test_replay_json_report_is_not_written_through_a_link(small_history, tmp_path, capsys):
    repo = small_history
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS")
    os.symlink(victim, repo.root / "report.json")
    assert replay_main(["--repo", str(repo.root), *ARGS, "--no-config",
                        "--json", str(repo.root / "report.json")]) == 0
    capsys.readouterr()
    assert victim.read_text() == "PRECIOUS"
    assert json.loads((repo.root / "report.json").read_text())["population"]["eligible"] == 1


@needs_symlinks
@requires_git
def test_replay_cache_is_not_opened_through_a_link(small_history, tmp_path, capsys):
    from carta.graph.eval.cache import default_cache_path
    from carta.graph.gitio import toplevel
    repo = small_history
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    os.symlink(victim, cache_dir / default_cache_path(toplevel(repo.root)).name)
    assert replay_main(["--repo", str(repo.root), "--resamples", "20", "--no-config",
                        "--cache-dir", str(cache_dir)]) == 0
    capsys.readouterr()
    assert victim.read_bytes() == b""
    assert ReplayCache(cache_dir / "linked.db").on_disk is True
    os.symlink(victim, cache_dir / "other.db-journal")
    assert ReplayCache(cache_dir / "other.db").on_disk is False
    assert victim.read_bytes() == b""


@needs_symlinks
@requires_git
def test_replay_does_not_read_a_symlinked_config(small_history, tmp_path, capsys):
    repo = small_history
    secret = tmp_path / "netrc"
    secret.write_text("machine example.invalid\nlogin me\npassword: s3cret: [oops\n")
    os.makedirs(repo.root / ".carta")
    os.symlink(secret, repo.root / ".carta" / "config.yaml")
    assert replay_main(["--repo", str(repo.root), *ARGS]) == 1
    err = capsys.readouterr().err
    assert "symbolic link" in err and "s3cret" not in err and "password" not in err
    assert replay_main(["--repo", str(repo.root), *ARGS, "--no-config"]) == 0
    capsys.readouterr()


@requires_git
def test_replay_config_errors_never_quote_the_file(small_history, capsys):
    repo = small_history
    repo.write(".carta/config.yaml", "graph:\n  doc_globs: ['*.md']\npassword: s3cret: [oops\n")
    assert replay_main(["--repo", str(repo.root), *ARGS]) == 1
    err = capsys.readouterr().err
    assert "line 3" in err and "s3cret" not in err and "password" not in err
    (repo.root / ".carta" / "config.yaml").write_bytes(b"\xff\xfe secret-bytes")
    assert replay_main(["--repo", str(repo.root), *ARGS]) == 1
    assert "secret-bytes" not in capsys.readouterr().err


@requires_git
def test_replay_ignores_repo_patterns_unless_trusted(small_history, capsys):
    repo = small_history
    repo.write("NOTES.md", "a" * 40 + "!\n")
    repo.commit("a doc line that makes a nested repeat take forever", 2)
    repo.write("README.md", "See `src/app.py`, updated twice.\n")
    repo.write("src/app.py", "x = 3\n")
    repo.commit("a scored commit, so every doc at its parent is scanned", 3)
    # (a+)+$ over forty "a" then "!" is two to the fortieth steps: if it ran, this would hang.
    repo.write(".carta/config.yaml",
               "graph:\n  patterns:\n    - {name: p, regex: '(a+)+$'}\n")
    assert replay_main(["--repo", str(repo.root), *ARGS, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["settings"]["config"] == ".carta/config.yaml (graph.patterns ignored)"
    repo.write(".carta/config.yaml",
               "graph:\n  patterns:\n    - {name: ticket, regex: 'BUG-[0-9]+'}\n")
    assert replay_main(["--repo", str(repo.root), *ARGS, "--trust-config", "--json"]) == 0
    trusted = json.loads(capsys.readouterr().out)
    assert trusted["settings"]["config"] == ".carta/config.yaml"
    assert trusted["settings"]["fingerprint"] != report["settings"]["fingerprint"]


def test_a_tampered_cache_entry_is_a_miss(tmp_path):
    path = tmp_path / "cache.db"
    with ReplayCache(path) as cache:
        cache.put_scan("b" * 40, "fp", [])
        cache.put_tokens("b" * 40, 1, {"a.py": 2})
    bomb = zlib.compress(b"[" + b"0," * (limits.MAX_CACHE_PAYLOAD_BYTES // 2 + 10) + b"0]")
    rows = {
        "bomb": bomb,
        "wrong_shape": zlib.compress(json.dumps([["raw", "code", 1, "ev", True]]).encode()),
        "wrong_ctx": zlib.compress(json.dumps([["raw", "nope", 1, "ev", True, ""]]).encode()),
        "wrong_type": zlib.compress(json.dumps([["raw", "code", "1", "ev", True, ""]]).encode()),
        "long_raw": zlib.compress(json.dumps(
            [["r" * (limits.MAX_LINE_LENGTH + 1), "code", 1, "ev", True, ""]]).encode()),
        "truncated": zlib.compress(b"[]")[:-3],
        "good": zlib.compress(json.dumps([["a/b.py", "code", 1, "ev", True, ""]]).encode()),
    }
    con = sqlite3.connect(str(path))
    for name, payload in rows.items():
        con.execute("INSERT OR REPLACE INTO scan VALUES (?, ?, ?)", (name, "fp", payload))
    con.execute("INSERT OR REPLACE INTO tokens VALUES (?, ?, ?)",
                ("badtok", 1, zlib.compress(json.dumps({"a": "many"}).encode())))
    con.commit()
    con.close()
    with ReplayCache(path) as cache:
        for name in rows:
            hit = cache.get_scan(name, "fp")
            assert (hit is not None) is (name == "good"), name
        assert cache.get_tokens("badtok", 1) is None
        assert cache.get_tokens("b" * 40, 1) == {"a.py": 2}


# -- the scan ----------------------------------------------------------------------

@requires_git
def test_run_scan_records_that_the_graph_check_failed(git_repo):
    from carta.scanner.scanner import run_scan

    def scan() -> dict:
        with patch("carta.scanner.scanner.suggest_related_for_all", return_value=[]), \
                patch("carta.scanner.scanner.get_file_last_commit_date", return_value=None):
            return run_scan(git_repo.root, {"docs_root": "docs/", "excluded_paths": [],
                                            "stale_threshold_days": 30},
                            git_repo.root / "scan-results.json", reference_date=date(2026, 1, 1))

    git_repo.write("docs/a.md", "---\nrelated: []\nlast_reviewed: 2026-01-01\n---\nText.\n")
    git_repo.commit("first", 0)
    assert scan()["stats"]["missing_path_check"] == "ok"
    with patch("carta.graph.scan.missing_path_findings", side_effect=RuntimeError("boom")):
        stats = scan()["stats"]
    assert (stats["missing_path_check"], stats["missing_path_mentions"]) == ("failed", 0)
