"""Co-change edges: the pair table (pure) and its incremental build into the store."""
from __future__ import annotations

import dataclasses
import json
import random
import subprocess

import pytest

from carta.graph import cochange as cochange_mod
from carta.graph.build import build_graph, graph_status
from carta.graph.cochange import (
    COCHANGE_EXTRACTOR, META_CURSOR, PairTable, build_cochange, countable_path,
)
from carta.graph.gitio import GitError
from carta.graph.settings import CochangeSettings, GraphConfigError, graph_settings
from carta.graph.store import EdgeStore, store_path
from carta.graph.tests.conftest import requires_git

PLAIN = CochangeSettings(max_files=30, min_support=1, min_score=0.0, half_life=0)
SHA = "{:040x}".format


def feed(table, commits, start=1):
    """``commits`` is a list of path lists (all modified); the first has no parent."""
    for i, paths in enumerate(commits, start):
        table.observe(SHA(i), [("M", p) for p in paths], has_parent=i > 1)
    return table


def weights(table, src):
    return {dst: round(score, 6) for dst, _n, score in table.neighbours(src)}


def cfg(**cochange):
    return {"graph": {"cochange": cochange}} if cochange else {}


# -- the pair table ----------------------------------------------------------------

def test_confidence_is_the_share_of_the_source_files_commits():
    table = feed(PairTable(PLAIN), [["root"], ["a", "b"], ["a", "b"], ["a"], ["a"]])
    assert table.score("b", "a") == (2, 1.0)        # every commit of b also changed a
    assert table.score("a", "b") == (2, 0.5)        # half of a's commits changed b
    assert table.score("a", "nope") == (0, 0.0)
    assert table.total == 4 and table.seq == 5


def test_a_commit_with_no_parent_is_never_counted():
    table = feed(PairTable(PLAIN), [["a", "b", "c"]])
    assert table.counts() == {"files": 0, "pairs": 0} and table.total == 0


def test_a_bulk_commit_creates_no_edges_and_no_counts():
    settings = dataclasses.replace(PLAIN, max_files=3)
    table = feed(PairTable(settings), [["root"], ["a", "b", "c", "d"], ["a", "b"]])
    assert table.bulk == 1 and table.total == 1
    assert sorted(table.files) == ["a", "b"]
    assert weights(table, "a") == {"b": 1.0} and weights(table, "c") == {}
    assert table.files["a"].commits == 1          # the sweep is not one of a's commits


def test_the_bulk_test_counts_every_changed_path_before_any_pair_is_formed(monkeypatch):
    settings = dataclasses.replace(PLAIN, max_files=3)
    table = PairTable(settings)
    table.observe(SHA(1), [("M", "root")], has_parent=False)
    # Three countable paths plus one that is not: four changed paths, so bulk.
    table.observe(SHA(2), [("M", "a"), ("M", "b"), ("M", "c"), ("M", "bad\nname")])
    assert table.bulk == 1 and not table.adj


def test_two_busy_but_independent_files_do_not_look_coupled():
    history = [["root"]]
    for i in range(40):
        history.append(["busy_a"] + (["busy_b"] if i < 3 else []))
    for i in range(37):
        history.append(["busy_b"])
    for i in range(10):
        history.append(["pair_c", "pair_d"])
    table = feed(PairTable(CochangeSettings(half_life=0)), history)     # the shipped floors
    assert table.score("busy_a", "busy_b") == (3, 3 / 40)
    assert weights(table, "busy_a") == {} and weights(table, "busy_b") == {}
    assert weights(table, "pair_c") == {"pair_d": 1.0}
    assert weights(table, "pair_d") == {"pair_c": 1.0}


def test_lift_discounts_a_file_that_changes_with_everything():
    history = [["root"]] + [["hub", f"f{i}"] for i in range(8)] + [["x", "y"], ["x", "y"]]
    conf = feed(PairTable(PLAIN), history)
    lift = feed(PairTable(dataclasses.replace(PLAIN, measure="lift")), history)
    assert conf.score("f0", "hub")[1] == conf.score("x", "y")[1] == 1.0
    assert lift.score("x", "y")[1] > lift.score("f0", "hub")[1]


def test_a_deleted_file_loses_its_history_and_starts_again_if_it_returns():
    table = feed(PairTable(PLAIN), [["root"], ["a", "b"], ["a", "b"]])
    table.observe(SHA(4), [("D", "b"), ("M", "c")])
    assert "b" not in table.files and weights(table, "a") == {}
    assert "b" in table.gone and "a" in table.dirty
    table.observe(SHA(5), [("A", "b"), ("M", "a")])
    assert table.score("b", "a") == (1, 1.0)
    assert table.files["b"].commits == 1


def test_a_pure_move_keeps_its_history():
    table = feed(PairTable(PLAIN), [["root"], ["old.py", "doc.md"], ["old.py", "doc.md"]])
    table.observe(SHA(4), [("D", "old.py"), ("A", "new.py")],
                  blobs={"new.py": "b" * 40}, removed={"old.py": "b" * 40})
    assert "old.py" not in table.files
    assert table.files["new.py"].commits == 3
    assert table.score("doc.md", "new.py") == (2, 1.0)
    assert table.score("new.py", "doc.md")[0] == 2
    assert "old.py" in table.gone and {"new.py", "doc.md"} <= table.dirty


@pytest.mark.parametrize("blobs, removed, follow", [
    ({"new.py": "c" * 40}, {"old.py": "b" * 40}, True),                     # edited while moving
    ({"new.py": "b" * 40}, {"old.py": "b" * 40}, False),                    # switched off
    ({"new.py": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"},
     {"old.py": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"}, True),         # the empty blob
    ({"new.py": "b" * 40, "copy.py": "b" * 40}, {"old.py": "b" * 40}, True),    # two candidates
])
def test_anything_but_a_one_to_one_pure_move_is_a_delete_and_an_add(blobs, removed, follow):
    settings = dataclasses.replace(PLAIN, follow_renames=follow)
    table = feed(PairTable(settings), [["root"], ["old.py", "doc.md"], ["old.py", "doc.md"]])
    changes = [("D", "old.py")] + [("A", path) for path in sorted(blobs)]
    table.observe(SHA(4), changes, blobs=blobs, removed=removed)
    assert "old.py" not in table.files
    assert table.files["new.py"].commits == 1
    assert table.score("doc.md", "new.py") == (0, 0.0)


def test_a_pair_below_the_floor_is_forgotten_after_the_pending_window():
    settings = dataclasses.replace(PLAIN, min_support=2, pending_window=3)
    quiet = [["other"]] * 4
    table = feed(PairTable(settings), [["root"], ["a", "b"]] + quiet + [["a", "b"]])
    assert table.score("a", "b")[0] == 1                # the first co-change had lapsed
    assert table.adj["a"]["b"].first == SHA(7)
    assert weights(table, "a") == {}
    table.observe(SHA(8), [("M", "a"), ("M", "b")])
    assert table.score("a", "b")[0] == 2 and weights(table, "a") == {"b": 0.666667}
    # Once a pair has reached the floor it is never forgotten.
    feed(table, quiet + [["a", "b"]], start=9)
    assert table.score("a", "b")[0] == 3


def test_decay_counts_an_old_co_change_less_than_a_recent_one():
    settings = dataclasses.replace(PLAIN, half_life=2)
    history = [["root"], ["a", "old"], ["a"], ["a"], ["a"], ["a", "new"]]
    table = feed(PairTable(settings), history)
    assert table.score("a", "old")[0] == table.score("a", "new")[0] == 1
    old, new = table.score("a", "old")[1], table.score("a", "new")[1]
    assert 0.0 < old < new < 1.0
    assert old == pytest.approx(new * 0.5 ** (4 / 2))
    flat = feed(PairTable(PLAIN), history)
    assert flat.score("a", "old")[1] == flat.score("a", "new")[1] == 0.2


def test_scores_never_exceed_one_under_decay():
    rng = random.Random(7)
    settings = dataclasses.replace(PLAIN, half_life=5)
    names = [f"f{i}" for i in range(8)]
    table = feed(PairTable(settings),
                 [["root"]] + [rng.sample(names, rng.randint(1, 4)) for _ in range(200)])
    for src in table.files:
        for _dst, _n, score in table.neighbours(src):
            assert 0.0 < score <= 1.0 + 1e-9


@pytest.mark.parametrize("path, ok", [
    ("src/a.py", True), ("docs/My Notes.md", True), ("-rf", True), ("é.md", True),
    ("a\nb", False), ("a\tb", False), ("a\x1b[31m", False), ("a\x7f", False),
    ("x" * 1025, False), ("", False), (".git/config", False), (".carta/x", False),
    ("bad\udcff", False), ("nested/", False),
    # Not plain printable text: C1 controls (CSI among them), a bidirectional
    # override, line and paragraph separators, a zero-width joiner, a no-break space.
    ("a\u009b31m.py", False), ("a\u0085b.py", False), ("a\u202e.py", False),
    ("a\u2028b.py", False), ("a\u2029b.py", False), ("a\u200db.py", False),
    ("a\u00a0b.py", False), ("日本語.md", True), ("a b/c d.py", True),
])
def test_countable_path(path, ok):
    assert countable_path(path) is ok


def test_uncountable_paths_never_enter_the_table():
    table = PairTable(PLAIN)
    table.observe(SHA(1), [("M", "root")], has_parent=False)
    table.observe(SHA(2), [("M", "a"), ("M", "evil\x1b]0;x\x07"), ("M", ".git/hooks/x")])
    assert sorted(table.files) == ["a"] and not table.adj


def test_edges_are_labelled_as_history_and_typed():
    table = feed(PairTable(PLAIN), [["root"], ["src/a.py", "docs/a.md"], ["src/a.py"]])
    edges = table.edges_from("src/a.py", lambda p: p.endswith(".md"))
    assert len(edges) == 1
    edge = edges[0]
    assert (edge.kind, edge.src, edge.dst, edge.src_type, edge.dst_type) == (
        "cochange", "src/a.py", "docs/a.md", "file", "doc")
    assert edge.evidence == "changed together in 1 of 2 commits" and edge.weight == 0.5
    assert json.loads(edge.meta) == {"support": 1, "commits": 2, "first": SHA(2), "last": SHA(2)}
    assert edge.extractor == COCHANGE_EXTRACTOR and edge.line == 0
    back = table.edges_from("docs/a.md", lambda p: p.endswith(".md"))
    assert (back[0].dst, back[0].dst_type, back[0].weight) == ("src/a.py", "file", 1.0)


def _dump(table):
    return {path: table.file_meta(path) for path in sorted(table.files)}


@pytest.mark.parametrize("settings", [
    PLAIN,
    dataclasses.replace(PLAIN, half_life=7),
    dataclasses.replace(PLAIN, min_support=2, pending_window=5),
    dataclasses.replace(PLAIN, min_support=3, pending_window=4, half_life=3, max_files=4),
])
def test_stopping_and_resuming_anywhere_gives_the_same_table(settings):
    """The property the incremental build rests on, on random histories."""
    rng = random.Random(130)
    names = [f"d{i % 3}/f{i}" for i in range(9)]
    for _round in range(25):
        commits = []
        alive = set()
        for i in range(1, 41):
            changes = []
            for path in rng.sample(names, rng.randint(1, 5)):
                if path in alive and rng.random() < 0.15:
                    changes.append(("D", path))
                    alive.discard(path)
                else:
                    changes.append(("M" if path in alive else "A", path))
                    alive.add(path)
            commits.append((SHA(i), changes))
        whole = PairTable(settings)
        for i, (sha, changes) in enumerate(commits):
            whole.observe(sha, changes, has_parent=i > 0)
        stop = rng.randint(0, len(commits))
        first = PairTable(settings)
        for i, (sha, changes) in enumerate(commits[:stop]):
            first.observe(sha, changes, has_parent=i > 0)
        resumed = PairTable.load(settings, first.seq, first.totals(), _dump(first))
        for i, (sha, changes) in enumerate(commits[stop:], stop):
            resumed.observe(sha, changes, has_parent=i > 0)
        assert _dump(resumed) == _dump(whole)
        assert resumed.totals() == whole.totals()
        is_doc = lambda p: p.startswith("d0/")                          # noqa: E731
        for path in whole.files:
            assert resumed.edges_from(path, is_doc) == whole.edges_from(path, is_doc)


@pytest.mark.parametrize("files, totals", [
    ({"a": "not json"}, None),
    ({"a": "[]"}, None),
    ({"a": '{"commits":1,"w":1.0,"seq":1,"pairs":{"ghost":[1,1.0,"x","y",1]}}'}, None),
    ({"a": '{"commits":-1,"w":1.0,"seq":1,"pairs":{}}'}, None),
    ({"a": '{"commits":1,"w":NaN,"seq":1,"pairs":{}}'}, None),
    ({"a": '{"commits":true,"w":1.0,"seq":1,"pairs":{}}'}, None),
    ({"a": '{"commits":1,"w":1.0,"seq":1,"pairs":[]}'}, None),
    ({"a": '{"commits":1,"w":1.0,"seq":1}'}, None),
    ({"bad\nname": '{"commits":1,"w":1.0,"seq":1,"pairs":{}}'}, None),
    ({"a": '{"commits":1,"w":1.0,"seq":1,"pairs":{"b":[1,1.0,"not-a-sha","' + "a" * 40 + '",1]}}',
      "b": '{"commits":1,"w":1.0,"seq":1,"pairs":{}}'}, None),
    ({}, "{}"),
    ({}, '{"total":1,"total_w":"x","total_seq":1,"bulk":0}'),
])
def test_loading_state_that_is_not_exactly_the_stored_shape_raises(files, totals):
    good = '{"bulk":0,"total":1,"total_seq":1,"total_w":1.0}'
    with pytest.raises((ValueError, KeyError, TypeError)):
        PairTable.load(PLAIN, 1, totals or good, files)


# -- settings ----------------------------------------------------------------------

def test_cochange_settings_defaults_and_overrides():
    default = graph_settings({}).cochange
    assert default == CochangeSettings()
    assert (default.enabled, default.measure, default.max_files) == (True, "confidence", 30)
    custom = graph_settings(cfg(measure="lift", half_life=0, min_support=3)).cochange
    assert (custom.measure, custom.half_life, custom.min_support) == ("lift", 0, 3)
    assert custom.max_files == 30
    settings = graph_settings({})
    assert default.fingerprint(settings, 1) != custom.fingerprint(settings, 1)
    assert default.fingerprint(settings, 1) != default.fingerprint(settings, 2)
    # mining settings do not touch the mention arm's fingerprint
    assert graph_settings(cfg(measure="lift")).fingerprint() == settings.fingerprint()


@pytest.mark.parametrize("bad", [
    {"measure": "pmi"}, {"measure": ["confidence"]}, {"max_files": 1}, {"max_files": 501},
    {"max_files": 2.5}, {"min_support": 0}, {"min_score": -0.1}, {"min_score": float("nan")},
    {"half_life": -1}, {"pending_window": float("inf")}, {"enabled": "no"},
    {"follow_renames": 1},
])
def test_bad_cochange_settings_are_config_errors(bad):
    with pytest.raises(GraphConfigError):
        graph_settings(cfg(**bad))


def test_cochange_section_must_be_a_mapping():
    with pytest.raises(GraphConfigError):
        graph_settings({"graph": {"cochange": ["enabled"]}})
    with pytest.raises(GraphConfigError):
        graph_settings({"graph": {"impact": {"combine": "average"}}})


# -- the build ---------------------------------------------------------------------

def cochange_edges(root):
    with EdgeStore(store_path(root)) as store:
        return [(e.src, e.dst, e.dst_type, e.weight, json.loads(e.meta)["support"])
                for e in store.iter_edges(kinds=("cochange",))]


def digest(root):
    with EdgeStore(store_path(root)) as store:
        return store.edges_digest((COCHANGE_EXTRACTOR,))


def seed_history(repo):
    """root; then src/a.py with docs/a.md twice, src/b.py alone, and a wide sweep."""
    repo.write("README.md", "# project\n")
    repo.commit("root", 0)
    repo.write("src/a.py", "a = 1\n")
    repo.write("docs/a.md", "about a\n")
    repo.commit("add a", 1)
    repo.write("src/a.py", "a = 2\n")
    repo.write("docs/a.md", "about a, again\n")
    repo.commit("change a", 2)
    repo.write("src/b.py", "b = 1\n")
    repo.commit("add b", 3)
    for i in range(6):
        repo.write(f"gen/f{i}.txt", "x\n")
    repo.write("src/a.py", "a = 3\n")
    repo.write("src/b.py", "b = 2\n")
    repo.commit("sweep", 4)
    return repo


SMALL = cfg(max_files=5, half_life=0)


@requires_git
def test_build_writes_both_directions_and_skips_the_sweep(git_repo):
    repo = seed_history(git_repo)
    report = build_graph(repo.root, SMALL)["cochange"]
    assert report["status"] == "ok" and report["full"] is True
    assert (report["commits"], report["counted"], report["bulk"]) == (5, 3, 1)
    assert report["new_commits"] == 5 and report["shallow"] is False
    assert report["head"] == repo.git("rev-parse", "HEAD").strip()
    assert cochange_edges(repo.root) == [
        ("docs/a.md", "src/a.py", "file", 1.0, 2),
        ("src/a.py", "docs/a.md", "doc", 1.0, 2),
    ]
    assert report["edges"] == 2 and report["pairs"] == 1 and report["files"] == 3
    # The sweep (8 paths) coupled nothing: a and b never share an edge.
    assert not any("src/b.py" in (src, dst) for src, dst, *_ in cochange_edges(repo.root))


@requires_git
def test_incremental_build_equals_a_full_rebuild(git_repo, make_git_repo):
    repo = seed_history(git_repo)
    seen = []
    build_graph(repo.root, SMALL)
    steps = [
        lambda: (repo.write("src/b.py", "b = 3\n"), repo.write("docs/b.md", "about b\n")),
        lambda: (repo.write("src/b.py", "b = 4\n"), repo.write("docs/b.md", "more b\n"),
                 repo.write("src/a.py", "a = 4\n")),
        lambda: (repo.git("mv", "src/b.py", "src/renamed.py"),),
        lambda: (repo.rm("docs/a.md"), repo.write("src/a.py", "a = 5\n")),
        lambda: (repo.write("src/renamed.py", "b = 5\n"), repo.write("docs/b.md", "b!\n")),
        lambda: [repo.write(f"gen/f{i}.txt", "y\n") for i in range(6)],
        lambda: (repo.write("docs/a.md", "back\n"), repo.write("src/a.py", "a = 6\n")),
    ]
    for day, step in enumerate(steps, 10):
        step()
        repo.commit(f"step {day}", day)
        report = build_graph(repo.root, SMALL)["cochange"]
        assert report["full"] is False and report["new_commits"] == 1
        seen.append(digest(repo.root))
        forced = build_graph(repo.root, SMALL, force=True)["cochange"]
        assert forced["full"] is True and forced["digest"] == seen[-1] == report["digest"]
    # The sixth step is a sweep (six paths, over the cap): it moves nothing. Every
    # other step does.
    assert seen[5] == seen[4]
    assert len(set(seen)) == len(seen) - 1
    # A second repository given the same history in one go agrees to the digest.
    again = build_graph(repo.root, SMALL)["cochange"]
    assert again["new_commits"] == 0 and again["written"] == 0 and again["digest"] == seen[-1]
    edges = cochange_edges(repo.root)
    # The move was pure, so src/renamed.py carries what src/b.py earned with docs/b.md:
    # three shared commits, out of the five that touched it under either name.
    assert ("src/renamed.py", "docs/b.md", "doc", 0.6, 3) in edges
    assert ("docs/b.md", "src/renamed.py", "file", 1.0, 3) in edges
    assert not any("src/b.py" in (src, dst) for src, dst, *_ in edges)
    # docs/a.md was deleted and came back: one shared commit since, not three.
    assert ("docs/a.md", "src/a.py", "file", 1.0, 1) in edges


@requires_git
def test_several_new_commits_in_one_build_equal_one_at_a_time(make_git_repo):
    one, many = make_git_repo("one"), make_git_repo("many")
    for repo in (one, many):
        seed_history(repo)
    build_graph(one.root, SMALL)
    for day in range(10, 16):
        for repo in (one, many):
            repo.write("src/a.py", f"a = {day}\n")
            if day % 2:
                repo.write("docs/a.md", f"day {day}\n")
            else:
                repo.write("src/c.py", f"c = {day}\n")
            repo.commit(f"day {day}", day)
        build_graph(one.root, SMALL)
    report = build_graph(many.root, SMALL)["cochange"]
    assert report["new_commits"] == 11 and report["digest"] == digest(one.root)


@requires_git
def test_build_reads_only_the_commits_after_the_cursor(git_repo, monkeypatch):
    repo = seed_history(git_repo)
    build_graph(repo.root, SMALL)
    repo.write("src/a.py", "a = 9\n")
    repo.commit("one more", 9)
    asked = []
    real = cochange_mod.iter_log_entries

    def spy(top, rows, **kwargs):
        asked.append(len(rows))
        return real(top, rows, **kwargs)

    monkeypatch.setattr(cochange_mod, "iter_log_entries", spy)
    assert build_graph(repo.root, SMALL)["cochange"]["new_commits"] == 1
    assert build_graph(repo.root, SMALL)["cochange"]["new_commits"] == 0
    assert asked == [1]


@requires_git
def test_a_shallow_clone_is_mined_as_far_as_its_history_goes(git_repo, tmp_path):
    repo = seed_history(git_repo)
    clone = tmp_path / "four"
    subprocess.run(["git", "clone", "-q", "--depth", "4", repo.root.as_uri(), str(clone)],
                   check=True, capture_output=True)
    report = build_graph(clone, SMALL)["cochange"]
    # The boundary commit looks like a root and is not counted; "change a" is.
    assert report["status"] == "ok" and report["shallow"] is True
    assert (report["commits"], report["counted"], report["edges"]) == (4, 2, 2)
    assert graph_status(clone, SMALL)["stale"] is False
    for depth, name in ((1, "one"), (2, "two")):
        clone = tmp_path / name
        subprocess.run(["git", "clone", "-q", "--depth", str(depth), repo.root.as_uri(),
                        str(clone)], check=True, capture_output=True)
        report = build_graph(clone, SMALL)["cochange"]
        assert report["status"] == "ok" and report["shallow"] is True
        assert report["commits"] == depth and report["edges"] == 0
        assert cochange_edges(clone) == []
        assert graph_status(clone, SMALL)["stale"] is False


@requires_git
def test_a_deepened_shallow_clone_is_mined_again_from_the_start(git_repo, tmp_path):
    repo = seed_history(git_repo)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", repo.root.as_uri(), str(clone)],
                   check=True, capture_output=True)
    build_graph(clone, SMALL)
    subprocess.run(["git", "fetch", "-q", "--unshallow"], cwd=str(clone), check=True,
                   capture_output=True)
    assert "cochange_behind" in graph_status(clone, SMALL)["stale_reasons"]
    report = build_graph(clone, SMALL)["cochange"]
    assert report["full"] is True and report["shallow"] is False
    build_graph(repo.root, SMALL)
    assert report["digest"] == digest(repo.root)


@requires_git
def test_no_history_is_no_edges_not_an_error(git_repo, tmp_path):
    report = build_graph(git_repo.root, {})["cochange"]             # a repository, no commit
    assert report["status"] == "no_history" and report["edges"] == 0 and report["commits"] == 0
    assert graph_status(git_repo.root, {})["stale"] is False
    plain = tmp_path / "plain"
    (plain / "docs").mkdir(parents=True)
    (plain / "docs" / "a.md").write_text("see `x.py`\n")
    report = build_graph(plain, {})                                  # not a repository at all
    assert report["cochange"]["status"] == "no_history" and report["docs"] == 1
    assert graph_status(plain, {})["stale"] is False


@requires_git
def test_rewritten_history_is_mined_again(git_repo):
    repo = seed_history(git_repo)
    build_graph(repo.root, SMALL)
    repo.write("src/a.py", "a = 7\n")
    repo.write("docs/a.md", "seven\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "--amend", "-m", "sweep, amended", date_index=4)
    assert "cochange_behind" in graph_status(repo.root, SMALL)["stale_reasons"]
    report = build_graph(repo.root, SMALL)["cochange"]
    assert report["full"] is True
    assert report["digest"] == build_graph(repo.root, SMALL, force=True)["cochange"]["digest"]


@requires_git
def test_changed_settings_are_a_full_rebuild_and_status_says_so(git_repo):
    repo = seed_history(git_repo)
    build_graph(repo.root, SMALL)
    wide = cfg(max_files=20, half_life=0)
    assert graph_status(repo.root, wide)["stale_reasons"] == ["cochange_settings_changed"]
    report = build_graph(repo.root, wide)["cochange"]
    assert report["full"] is True and report["bulk"] == 0
    assert ("src/a.py", "src/b.py", "file", 0.333333, 1) in cochange_edges(repo.root)
    assert graph_status(repo.root, wide)["stale"] is False


@requires_git
def test_disabling_removes_the_rows_and_skipping_leaves_them(git_repo):
    repo = seed_history(git_repo)
    build_graph(repo.root, SMALL)
    repo.write("src/a.py", "a = 9\n")
    repo.commit("later", 9)
    skipped = build_graph(repo.root, SMALL, cochange=False)["cochange"]
    assert skipped == {"enabled": True, "status": "skipped"}
    assert len(cochange_edges(repo.root)) == 2
    assert "cochange_behind" in graph_status(repo.root, SMALL)["stale_reasons"]
    off = cfg(enabled=False)
    assert graph_status(repo.root, off)["stale_reasons"] == ["cochange_disabled"]
    report = build_graph(repo.root, off)
    assert report["cochange"]["status"] == "disabled" and report["by_kind"].get("cochange") is None
    assert cochange_edges(repo.root) == []
    status = graph_status(repo.root, off)
    assert status["stale"] is False and status["cochange"]["enabled"] is False


@requires_git
def test_a_git_failure_leaves_the_store_as_it_was_and_still_stale(git_repo, monkeypatch):
    repo = seed_history(git_repo)
    build_graph(repo.root, SMALL)
    before = digest(repo.root)
    repo.write("src/a.py", "a = 9\n")
    repo.write("docs/a.md", "nine\n")
    repo.commit("later", 9)

    def broken(top, rows, **kwargs):
        raise GitError("git went away")

    def noisy(top, rows, **kwargs):
        raise GitError("bad name \x1b[31m\n" + "x" * 500)

    monkeypatch.setattr(cochange_mod, "iter_log_entries", noisy)
    detail = build_graph(repo.root, SMALL)["cochange"]["detail"]
    assert detail.startswith("bad name ?[31m?x") and len(detail) == 300

    monkeypatch.setattr(cochange_mod, "iter_log_entries", broken)
    report = build_graph(repo.root, SMALL)
    assert report["cochange"] == {"enabled": True, "status": "failed", "detail": "git went away"}
    assert report["docs"] == 2                          # the mention pass still ran
    assert digest(repo.root) == before
    with EdgeStore(store_path(repo.root)) as store:
        assert store.get_meta(META_CURSOR) != repo.git("rev-parse", "HEAD").strip()
    assert "cochange_behind" in graph_status(repo.root, SMALL)["stale_reasons"]
    monkeypatch.undo()
    assert build_graph(repo.root, SMALL)["cochange"]["new_commits"] == 1
    assert graph_status(repo.root, SMALL)["stale"] is False


@requires_git
def test_a_failure_part_way_through_a_batch_writes_nothing(git_repo, monkeypatch):
    repo = seed_history(git_repo)
    monkeypatch.setattr(cochange_mod, "_LOG_BATCH", 2)
    calls = []
    real = cochange_mod.iter_log_entries

    def second_batch_fails(top, rows, **kwargs):
        calls.append(len(rows))
        if len(calls) == 2:
            raise GitError("cut off")
        return real(top, rows, **kwargs)

    monkeypatch.setattr(cochange_mod, "iter_log_entries", second_batch_fails)
    assert build_graph(repo.root, SMALL)["cochange"]["status"] == "failed"
    with EdgeStore(store_path(repo.root)) as store:
        assert not store.get_meta(META_CURSOR) and not store.source_meta(COCHANGE_EXTRACTOR)
    monkeypatch.setattr(cochange_mod, "iter_log_entries", real)
    assert build_graph(repo.root, SMALL)["cochange"]["commits"] == 5


@requires_git
def test_damaged_stored_state_is_rebuilt_from_history_not_trusted(git_repo):
    repo = seed_history(git_repo)
    good = build_graph(repo.root, SMALL)["cochange"]["digest"]
    with EdgeStore(store_path(repo.root)) as store:
        store.set_source_meta(COCHANGE_EXTRACTOR, "src/a.py", '{"commits": "many"}')
    repo.write("src/z.py", "z\n")
    repo.commit("z", 9)
    report = build_graph(repo.root, SMALL)["cochange"]
    assert report["full"] is True and report["status"] == "ok"
    repo.git("reset", "-q", "--hard", "HEAD~1")
    assert build_graph(repo.root, SMALL)["cochange"]["digest"] == good


@requires_git
def test_a_merge_commit_is_one_transaction_holding_the_whole_branch(git_repo):
    repo = git_repo
    repo.write("README.md", "x\n")
    repo.commit("root", 0)
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/a.py", "a\n")
    repo.commit("part one", 1)
    repo.write("docs/a.md", "a\n")
    repo.commit("part two", 2)
    repo.git("checkout", "-q", "main")
    repo.write("other.txt", "o\n")
    repo.commit("meanwhile", 3)
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "merge feature", date_index=4)
    report = build_graph(repo.root, cfg(half_life=0))["cochange"]
    assert report["commits"] == 3                       # root, meanwhile, the merge
    assert ("src/a.py", "docs/a.md", "doc", 1.0, 1) in cochange_edges(repo.root)


@requires_git
def test_a_carta_root_below_the_git_root_sees_its_own_paths_only(git_repo):
    repo = git_repo
    repo.write("top.txt", "t\n")
    repo.commit("root", 0)
    for day in (1, 2):
        repo.write("pkg/src/a.py", f"a = {day}\n")
        repo.write("pkg/docs/a.md", f"day {day}\n")
        repo.write("elsewhere/x.py", f"x = {day}\n")
        repo.commit(f"day {day}", day)
    report = build_graph(repo.root / "pkg", cfg(half_life=0))["cochange"]
    assert report["files"] == 2
    assert cochange_edges(repo.root / "pkg") == [
        ("docs/a.md", "src/a.py", "file", 1.0, 2),
        ("src/a.py", "docs/a.md", "doc", 1.0, 2),
    ]


@requires_git
def test_hostile_file_names_are_read_exactly_and_never_stored(git_repo):
    repo = git_repo
    repo.write("README.md", "x\n")
    repo.commit("root", 0)
    names = ["line\nbreak.py", "tab\tname.py", "\x01ctl.py", "-dash.py", "--end-of-options",
             "sp ace.md", "quote\".py", "dollar$(x).py", "bidi\u202eevil.py",
             "csi\u009b31m.py", "sep\u2028line.py"]
    made = []
    for name in names:
        try:
            repo.write(name, "x\n")
            made.append(name)
        except OSError:
            pass
    if len(made) < 4:
        pytest.skip("this filesystem refuses the odd file names")
    repo.write("plain.py", "p\n")
    repo.commit("odd names\n\nwith a\x01body\tand tabs", 1)
    for name in made:
        repo.write(name, "y\n")
    repo.write("plain.py", "q\n")
    repo.commit("again", 2)
    report = build_graph(repo.root, cfg(half_life=0))["cochange"]
    assert report["status"] == "ok" and report["counted"] == 2
    with EdgeStore(store_path(repo.root)) as store:
        stored = set(store.source_meta(COCHANGE_EXTRACTOR))
    printable = {n for n in made if n.isprintable()} | {"plain.py"}
    assert stored == printable
    assert all(dst in printable for _src, dst, *_ in cochange_edges(repo.root))
    assert ("plain.py", "-dash.py", "file", 1.0, 2) in cochange_edges(repo.root)


@requires_git
def test_status_reports_the_history_cursor(git_repo):
    repo = seed_history(git_repo)
    assert graph_status(repo.root, SMALL)["cochange"] == {
        "enabled": True, "commits": 0, "head": "", "stale": False, "reason": "",
        "capped": 0}
    build_graph(repo.root, SMALL)
    head = repo.git("rev-parse", "HEAD").strip()
    status = graph_status(repo.root, SMALL)
    assert status["cochange"] == {"enabled": True, "commits": 5, "head": head, "stale": False,
                                  "reason": "", "capped": 0}
    assert status["stale"] is False and status["by_kind"]["cochange"] == 2
    repo.write("src/a.py", "a = 9\n")
    repo.commit("later", 9)
    status = graph_status(repo.root, SMALL)
    assert status["stale_reasons"] == ["cochange_behind"]
    assert status["cochange"]["head"] == head


@requires_git
def test_the_mention_digest_does_not_move_with_history(git_repo):
    repo = seed_history(git_repo)
    first = build_graph(repo.root, SMALL)
    repo.write("src/b.py", "b = 9\n")
    repo.commit("code only", 9)
    second = build_graph(repo.root, SMALL)
    assert second["digest"] == first["digest"]
    assert second["cochange"]["digest"] != first["cochange"]["digest"]


@requires_git
def test_build_cochange_is_reachable_on_its_own(git_repo):
    repo = seed_history(git_repo)
    settings = graph_settings(SMALL)
    assert build_cochange(repo.root, settings)["edges"] == 2
    off = graph_settings(cfg(enabled=False))
    assert build_cochange(repo.root, off)["status"] == "disabled"
    assert cochange_edges(repo.root) == []


# -- review findings (#130) --------------------------------------------------------

def _break_head(repo):
    """Point the branch at an object that is not there, as a damaged repository would."""
    ref = repo.root / ".git" / "refs" / "heads" / "main"
    if not ref.is_file():
        pytest.skip("this git does not keep the branch as a loose ref")
    saved = ref.read_bytes()
    ref.write_bytes(b"1" * 40 + b"\n")
    return ref, saved


@requires_git
def test_a_history_git_cannot_list_is_a_failure_not_an_empty_history(git_repo, monkeypatch):
    repo = seed_history(git_repo)
    assert build_graph(repo.root, SMALL)["cochange"]["edges"] == 2
    before = digest(repo.root)
    head = repo.git("rev-parse", "HEAD").strip()
    ref, saved = _break_head(repo)

    report = build_graph(repo.root, SMALL)
    assert report["cochange"]["status"] == "failed" and report["cochange"]["detail"]
    assert report["docs"] == 2                          # the mention pass still ran
    assert digest(repo.root) == before
    with EdgeStore(store_path(repo.root)) as store:
        assert store.get_meta(META_CURSOR) == head
    status = graph_status(repo.root, SMALL)
    assert status["stale"] is True and status["stale_reasons"] == ["cochange_git_failed"]
    assert status["by_kind"]["cochange"] == 2
    with pytest.raises(GitError):
        build_cochange(repo.root, graph_settings(SMALL))

    ref.write_bytes(saved)
    assert graph_status(repo.root, SMALL)["stale"] is False

    def timed_out(top, rev="HEAD"):
        raise GitError("git timed out")

    monkeypatch.setattr(cochange_mod, "first_parent_rows", timed_out)
    assert build_graph(repo.root, SMALL)["cochange"] == {
        "enabled": True, "status": "failed", "detail": "git timed out"}
    assert digest(repo.root) == before
    assert graph_status(repo.root, SMALL)["stale_reasons"] == ["cochange_git_failed"]


@requires_git
def test_a_repository_git_refuses_is_a_failure_not_a_plain_directory(git_repo, tmp_path):
    repo = seed_history(git_repo)
    build_graph(repo.root, SMALL)
    before = digest(repo.root)
    (repo.root / ".git").rename(tmp_path / "moved.git")
    (repo.root / ".git").write_text("gitdir: /nonexistent/elsewhere\n")
    assert build_graph(repo.root, SMALL)["cochange"]["status"] == "failed"
    assert digest(repo.root) == before
    assert graph_status(repo.root, SMALL)["stale_reasons"] == ["cochange_git_failed"]
    # With no .git entry at all it is simply not a repository: no history, no rows.
    (repo.root / ".git").unlink()
    assert build_graph(repo.root, SMALL)["cochange"]["status"] == "no_history"
    assert cochange_edges(repo.root) == []


@requires_git
def test_an_orphan_branch_is_an_empty_history(git_repo):
    repo = seed_history(git_repo)
    build_graph(repo.root, SMALL)
    repo.git("checkout", "-q", "--orphan", "fresh")
    report = build_graph(repo.root, SMALL)["cochange"]
    assert report["status"] == "no_history" and report["edges"] == 0
    assert cochange_edges(repo.root) == []
    assert graph_status(repo.root, SMALL)["stale"] is False


def _bulk_history(repo, rng):
    """A history whose bulk commits also delete files and move them without edits."""
    names = [f"src/f{i}.py" for i in range(12)]
    for i, name in enumerate(names):
        repo.write(name, f"file {i}\n")
    repo.commit("root", 0)
    live = list(names)
    for day in range(1, 26):
        kind = rng.choice(["small", "small", "small", "sweep", "exodus"])
        if kind == "small" or len(live) < 8:
            for name in rng.sample(live, rng.randint(1, 3)):
                repo.write(name, f"{name} on day {day}\n")
        elif kind == "sweep":                       # every file edited: over any small limit
            for name in live:
                repo.write(name, f"{name} swept on day {day}\n")
        else:                                       # a mass move, some deletions, one copy pair
            for name in list(live[:6]):
                fresh = name.replace("src/", f"moved{day}/")
                (repo.root / fresh).parent.mkdir(parents=True, exist_ok=True)
                (repo.root / name).rename(repo.root / fresh)
                live[live.index(name)] = fresh
            gone = live.pop()
            repo.rm(gone)
            twin = live.pop()
            body = (repo.root / twin).read_bytes()
            repo.rm(twin)
            for copy in (f"twin{day}/a.py", f"twin{day}/b.py"):     # one old, two new: no move
                (repo.root / copy).parent.mkdir(parents=True, exist_ok=True)
                (repo.root / copy).write_bytes(body)
                live.append(copy)
            repo.write(f"new{day}.py", f"new on {day}\n")
            live.append(f"new{day}.py")
        repo.commit(f"day {day}", day)


@requires_git
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_bulk_commits_read_without_their_paths_give_the_same_table(git_repo, seed):
    from carta.graph.gitio import first_parent_rows, log_entries
    _bulk_history(git_repo, random.Random(seed))
    rows = first_parent_rows(git_repo.root)
    settings = dataclasses.replace(PLAIN, max_files=4)
    whole = PairTable(settings)                     # every change of every commit in hand
    for entry in log_entries(git_repo.root, rows):
        cochange_mod.observe_entry(whole, entry)
    streamed = PairTable(settings)
    seen = list(cochange_mod.mine_rows(git_repo.root, rows, [streamed]))
    assert whole.bulk >= 3 and whole.total >= 3
    assert _dump(streamed) == _dump(whole)
    assert (streamed.seq, streamed.total, streamed.bulk) == (whole.seq, whole.total, whole.bulk)
    assert streamed.gone - set(streamed.files) == whole.gone - set(whole.files)
    # The limit held while reading: no entry ever carried more paths than it allows.
    assert max(len(entry.changes) for entry in seen) <= settings.max_files
    assert any(entry.truncated and entry.n_changes > settings.max_files for entry in seen)


@requires_git
def test_a_commit_over_the_limit_is_counted_and_never_collected(git_repo):
    from carta.graph.gitio import first_parent_rows, iter_log_entries
    repo = git_repo
    repo.write("keep.py", "k\n")
    repo.commit("root", 0)
    for i in range(40):
        repo.write(f"gen/f{i:02d}.txt", "x\n")
    repo.write("other/z.py", "z\n")
    repo.commit("wide", 1)
    repo.write("keep.py", "k2\n")
    repo.commit("narrow", 2)
    repo.commit("empty", 3)
    rows = first_parent_rows(repo.root)
    root, wide, narrow, empty = iter_log_entries(repo.root, rows, max_changes=5)
    assert (wide.truncated, wide.n_changes, wide.changes, wide.blobs, wide.removed) == (
        True, 41, (), {}, {})
    assert (root.truncated, root.n_changes) == (False, 1)
    assert narrow.changes == (("M", "keep.py"),) and not narrow.truncated
    assert (empty.changes, empty.n_changes, empty.truncated) == ((), 0, False)
    # A prefix is applied before the count, so a wide change elsewhere is not this root's bulk.
    _r, wide, _n, _e = iter_log_entries(repo.root, rows, max_changes=5, prefix="other/")
    assert (wide.truncated, wide.changes) == (False, (("A", "other/z.py"),))
    exact = list(iter_log_entries(repo.root, rows, max_changes=41))[1]
    assert not exact.truncated and exact.n_changes == 41


@requires_git
def test_a_build_with_nothing_new_does_not_read_the_pair_state(git_repo, monkeypatch):
    repo = seed_history(git_repo)
    first = build_graph(repo.root, SMALL)["cochange"]

    def loaded(*args, **kwargs):
        raise AssertionError("the pair state was loaded for a build with no new commit")

    real_source_meta = EdgeStore.source_meta

    def pair_state(store, extractor):
        # Only the history pass's own rows: the symbol pass (#131) asks for its own.
        if extractor == cochange_mod.COCHANGE_EXTRACTOR:
            loaded()
        return real_source_meta(store, extractor)

    monkeypatch.setattr(PairTable, "load", loaded)
    monkeypatch.setattr(EdgeStore, "source_meta", pair_state)
    again = build_graph(repo.root, SMALL, cochange=True)["cochange"]
    assert again == dict(first, new_commits=0, written=0, full=False)
    assert again["digest"] == digest(repo.root) and again["edges"] == 2
    monkeypatch.undo()
    # A forced build, and one with a new commit, still read and rewrite it.
    assert build_cochange(repo.root, graph_settings(SMALL), force=True)["full"] is True
    repo.write("src/a.py", "a = 9\n")
    repo.commit("later", 9)
    later = build_graph(repo.root, SMALL)["cochange"]
    assert later["new_commits"] == 1 and later["commits"] == 6
    assert build_graph(repo.root, SMALL)["cochange"] == dict(later, new_commits=0, written=0)


@requires_git
def test_a_summary_that_is_not_on_record_falls_back_to_reading_the_state(git_repo):
    repo = seed_history(git_repo)
    first = build_graph(repo.root, SMALL)["cochange"]
    for damaged in ("", "{}", '{"counted": -1}', "not json"):
        with EdgeStore(store_path(repo.root)) as store:
            store.set_meta(cochange_mod.META_SUMMARY, damaged)
        assert build_graph(repo.root, SMALL)["cochange"] == dict(
            first, new_commits=0, written=0, full=False)
        with EdgeStore(store_path(repo.root)) as store:
            assert json.loads(store.get_meta(cochange_mod.META_SUMMARY))["edges"] == 2
