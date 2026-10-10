"""The temporal holdout: edges frozen at a cut, scored on the commits after it."""
from __future__ import annotations

import json
import os

import pytest

from carta.graph.eval import holdout as holdout_mod
from carta.graph.eval.holdout import (
    HoldoutError, format_holdout, load_external_graph, main, parse_variants, run_holdout,
)
from carta.graph.settings import CochangeSettings, GraphConfigError
from carta.graph.tests.conftest import requires_git

FLAT = CochangeSettings(half_life=0, min_score=0.0)
OPTS = dict(variants={"cochange": FLAT}, resamples=50, min_population=1)


def touch(repo, day, *paths):
    for path in paths:
        repo.write(path, f"{day}\n")
    return repo.commit(f"day {day}", day)


@pytest.fixture
def repo(git_repo):
    """Before the cut: (a, b) change together, c changes alone and often.

    After it: a with b twice; a with c once; a with ``late`` (born after the
    cut); b alone; and c with d, a pair that only ever meets after the cut.
    """
    r = git_repo
    r.commits = {}
    touch(r, 0, "src/a.py", "src/b.py", "src/c.py", "lib/d.py")
    for day in (1, 2, 3):
        touch(r, day, "src/a.py", "src/b.py")
    for day in (4, 5, 6, 7):
        touch(r, day, "src/c.py")
    r.commits["cut"] = touch(r, 8, "lib/d.py")
    r.commits["ab1"] = touch(r, 10, "src/a.py", "src/b.py")
    r.commits["ab2"] = touch(r, 11, "src/a.py", "src/b.py")
    r.commits["ac"] = touch(r, 12, "src/a.py", "src/c.py")
    r.commits["late"] = touch(r, 13, "src/a.py", "src/late.py")
    r.commits["solo"] = touch(r, 14, "src/b.py")
    r.commits["cd1"] = touch(r, 15, "src/c.py", "lib/d.py")
    r.commits["cd2"] = touch(r, 16, "src/c.py", "lib/d.py")
    return r


@requires_git
def test_population_and_scores(repo):
    report = run_holdout(repo.root, cut=repo.commits["cut"], per_commit=True, **OPTS)
    pop = report["population"]
    assert (pop["commits"], pop["train_commits"], pop["test_commits"]) == (16, 9, 7)
    assert pop["eligible"] == 5 and pop["queries"] == 10
    assert pop["skipped"] == {"single_file": 1, "bulk": 0, "unknown_files": 1}
    assert pop["files_at_cut"] == 4 and pop["gold_files_mean"] == 1.0
    assert report["repo"]["cut"] == repo.commits["cut"]
    assert report["train"]["cochange"] == {"files": 4, "pairs": 1, "counted": 8, "bulk": 0,
                                           "edges": 2}
    method = report["methods"]["cochange"]
    # Right on the two (a, b) commits, nothing to say on the other three.
    assert method["mrr"] == pytest.approx(2 / 5) and method["recall_at"]["1"] == pytest.approx(0.4)
    assert method["coverage"] == pytest.approx((1 + 1 + 0.5 + 0 + 0) / 5)
    assert set(report["methods"]) == {"cochange", "popularity", "directory"}
    assert set(report["paired"]["cochange"]) == {"popularity", "directory"}
    rr = {row["commit"]: row["rr"]["cochange"] for row in report["per_commit"]}
    assert rr == {repo.commits["ab1"]: 1.0, repo.commits["ab2"]: 1.0, repo.commits["ac"]: 0.0,
                  repo.commits["cd1"]: 0.0, repo.commits["cd2"]: 0.0}


@requires_git
def test_nothing_after_the_cut_reaches_a_method(repo):
    """(c, d) meet twice after the cut and never before: they must stay unpredicted."""
    report = run_holdout(repo.root, cut=repo.commits["cut"], per_commit=True, **OPTS)
    rr = {row["commit"]: row["rr"]["cochange"] for row in report["per_commit"]}
    assert rr[repo.commits["cd1"]] == 0.0 and rr[repo.commits["cd2"]] == 0.0
    # Move the cut past the first meeting and the second one is predicted.
    later = run_holdout(repo.root, cut=repo.commits["cd1"], per_commit=True, **OPTS)
    rr = {row["commit"]: row["rr"]["cochange"] for row in later["per_commit"]}
    assert rr == {repo.commits["cd2"]: 1.0}
    assert later["train"]["cochange"]["counted"] == 14


@requires_git
def test_the_same_cut_gives_the_same_table_whatever_follows_it(repo, make_git_repo):
    before = run_holdout(repo.root, cut=repo.commits["cut"], **OPTS)
    touch(repo, 20, "src/a.py", "src/c.py")
    touch(repo, 21, "src/a.py", "src/c.py")
    after = run_holdout(repo.root, cut=repo.commits["cut"], **OPTS)
    assert after["train"] == before["train"]
    assert after["population"]["files_at_cut"] == before["population"]["files_at_cut"]
    assert after["population"]["eligible"] == before["population"]["eligible"] + 2


@requires_git
def test_a_file_born_after_the_cut_is_neither_seed_nor_answer(repo):
    report = run_holdout(repo.root, cut=repo.commits["cut"], per_commit=True, **OPTS)
    assert repo.commits["late"] not in {row["commit"] for row in report["per_commit"]}
    assert report["population"]["skipped"]["unknown_files"] == 1


@requires_git
def test_baselines(repo):
    report = run_holdout(repo.root, cut=repo.commits["cut"], per_commit=True, **OPTS)
    # Popularity at the cut: c 4, a 3, b 3, d 1 (the root commit is not counted).
    by = {row["commit"]: row["rr"] for row in report["per_commit"]}
    # ab: seed a -> ranking c, b, d: b at 2. seed b -> c, a, d: a at 2.
    assert by[repo.commits["ab1"]]["popularity"] == 0.5
    # ac: seed a -> c first (1.0); seed c -> a first (1.0).
    assert by[repo.commits["ac"]]["popularity"] == 1.0
    # directory: the other files of src/, most changed first.
    assert by[repo.commits["ab1"]]["directory"] == pytest.approx((0.5 + 0.5) / 2)
    assert by[repo.commits["cd1"]]["directory"] == 0.0
    assert report["methods"]["popularity"]["coverage"] == 1.0
    window = run_holdout(repo.root, cut=repo.commits["cut"], popularity_window=1,
                         per_commit=True, **OPTS)
    # Only the last training commit (d alone) is counted: d, then the rest by path.
    by = {row["commit"]: row["rr"] for row in window["per_commit"]}
    assert by[repo.commits["cd1"]]["popularity"] == pytest.approx((1.0 + 1 / 3) / 2)


@requires_git
def test_variants_are_scored_on_one_population(repo):
    variants = {"confidence": FLAT, "floor2": CochangeSettings(half_life=0, min_support=4),
                "lift": CochangeSettings(half_life=0, min_score=0.0, measure="lift")}
    report = run_holdout(repo.root, cut=repo.commits["cut"], variants=variants, resamples=50)
    assert {m["n"] for m in report["methods"].values()} == {5}
    assert report["methods"]["floor2"]["mrr"] == 0.0        # three co-changes, floor of four
    assert report["train"]["floor2"]["edges"] == 0
    assert set(report["paired"]["lift"]) == {"popularity", "directory", "confidence", "floor2"}
    assert report["settings"]["cochange"]["lift"]["measure"] == "lift"
    assert report["population"]["conclusive"] is False     # five commits, thirty needed
    assert "INCONCLUSIVE" in format_holdout(report)


@requires_git
def test_an_external_graph_is_scored_on_the_same_queries(repo, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"edges": [["src/c.py", "lib/d.py"], ["src/a.py", "src/c.py", 2]]}))
    bad = tmp_path / "bad.tsv"
    bad.write_text("# an import graph\nsrc/a.py\tlib/d.py\t3\nsrc/a.py\tsrc/late.py\n")
    graphs = {"good": load_external_graph(good), "bad": load_external_graph(bad)}
    report = run_holdout(repo.root, cut=repo.commits["cut"], graphs=graphs, per_commit=True,
                         **OPTS)
    assert list(report["methods"]) == ["cochange", "graph:bad", "graph:good", "popularity",
                                       "directory"]
    assert {m["n"] for m in report["methods"].values()} == {5}
    by = {row["commit"]: row["rr"] for row in report["per_commit"]}
    # Seed d finds c first; seed c finds a (weight 2) before d (weight 1).
    assert by[repo.commits["cd1"]]["graph:good"] == 0.75
    assert by[repo.commits["ac"]]["graph:good"] == 1.0
    assert by[repo.commits["ab1"]]["graph:good"] == 0.0
    assert report["methods"]["graph:bad"]["mrr"] == 0.0
    info = report["settings"]["graphs"]
    assert info["graph:good"] == {"edges": 2, "rejected": 0, "nodes": 3, "directed": False,
                                  "known_nodes": 3}
    # src/late.py did not exist at the cut, so it is never a candidate.
    assert info["graph:bad"]["nodes"] == 3 and info["graph:bad"]["known_nodes"] == 2
    assert "graph:good" in report["paired"] and "cochange" in report["paired"]["graph:good"]
    text = format_holdout(report)
    assert "Not checked for leakage" in text and "graph:good" in text


def test_external_graph_formats(tmp_path):
    def load(name, text, **kw):
        path = tmp_path / name
        path.write_text(text)
        return load_external_graph(path, **kw)

    pairs, info = load("a.json", '[["a.py", "b.py"], ["./b.py", "c.py", 0.5]]')
    assert pairs == {"a.py": {"b.py": 1.0}, "b.py": {"a.py": 1.0, "c.py": 0.5},
                     "c.py": {"b.py": 0.5}}
    assert info == {"edges": 2, "rejected": 0, "nodes": 3, "directed": False}
    adjacency, _ = load("b.json", '{"a.py": ["b.py"], "c.py": {"a.py": 2}}', directed=True)
    assert adjacency == {"a.py": {"b.py": 1.0}, "c.py": {"a.py": 2.0}}
    objects, _ = load("c.json", '{"edges": [{"src": "a.py", "dst": "b.py", "weight": 3}]}',
                      directed=True)
    assert objects == {"a.py": {"b.py": 3.0}}
    text, info = load("d.tsv", "a.py\tb.py\n\n# note\na.py\tb.py\t4\r\nb.py\ta.py\t2\n",
                      directed=True)
    assert text == {"a.py": {"b.py": 4.0}, "b.py": {"a.py": 2.0}} and info["edges"] == 3
    windows, _ = load("e.txt", "src\\a.py\tsrc\\b.py\n", directed=True)
    assert windows == {"src/a.py": {"src/b.py": 1.0}}


def test_external_graph_rejects_rows_it_cannot_trust(tmp_path):
    path = tmp_path / "g.json"
    rows = [["a.py", "a.py"], ["/etc/passwd", "a.py"], ["../x", "a.py"], ["a.py", "b\nc"],
            ["a.py", 7], ["a.py", "b.py", "heavy"], ["a.py", "b.py", -1], ["a.py", "b.py", 1e999],
            ["a.py"], "a.py", ["a.py", "b.py", True], [".git/config", "a.py"],
            ["a.py", "ok.py", "2.5"]]
    path.write_text(json.dumps(rows))
    graph, info = load_external_graph(path, directed=True)
    assert graph == {"a.py": {"ok.py": 2.5}}
    assert info["edges"] == 1 and info["rejected"] == len(rows) - 1


@pytest.mark.parametrize("text", ["{", '"just a string"', "7", '{"edges": 1, "a": 2}'])
def test_external_graph_that_is_not_a_graph(tmp_path, text):
    path = tmp_path / "g.json"
    path.write_text(text)
    try:
        graph, info = load_external_graph(path)
    except HoldoutError:
        return
    assert graph == {} and info["edges"] == 0


def test_external_graph_bounds_are_checked_before_parsing(tmp_path, monkeypatch):
    path = tmp_path / "g.tsv"
    path.write_text("a.py\tb.py\nb.py\tc.py\nc.py\td.py\n")
    monkeypatch.setattr(holdout_mod, "MAX_EXTERNAL_GRAPH_EDGES", 2)
    with pytest.raises(HoldoutError, match="more than 2 edges"):
        load_external_graph(path)
    monkeypatch.setattr(holdout_mod, "MAX_EXTERNAL_GRAPH_BYTES", 10)
    with pytest.raises(HoldoutError, match="larger than"):
        load_external_graph(path)
    with pytest.raises(HoldoutError, match="could not read"):
        load_external_graph(tmp_path / "absent.tsv")
    with pytest.raises(HoldoutError, match="not a regular file"):
        load_external_graph(tmp_path)
    binary = tmp_path / "b.tsv"
    binary.write_bytes(b"\xff\xfe\x00")
    monkeypatch.setattr(holdout_mod, "MAX_EXTERNAL_GRAPH_BYTES", 1000)
    with pytest.raises(HoldoutError, match="UTF-8"):
        load_external_graph(binary)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_external_graph_that_is_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "pipe.tsv"
    os.mkfifo(fifo)
    with pytest.raises(HoldoutError, match="not a regular file"):
        load_external_graph(fifo)


@requires_git
def test_cut_errors_and_edges_of_the_input_space(repo, git_repo, tmp_path):
    with pytest.raises(HoldoutError, match="does not name a commit"):
        run_holdout(repo.root, cut="nope", **OPTS)
    with pytest.raises(HoldoutError, match="does not name a commit"):
        run_holdout(repo.root, cut="--output=/tmp/x", **OPTS)
    for share in (0.0, 1.0, 1.5, 1):
        with pytest.raises(HoldoutError, match="between 0 and 1"):
            run_holdout(repo.root, cut_share=share, **OPTS)
    with pytest.raises(HoldoutError, match="k values"):
        run_holdout(repo.root, ks=(0, 5), **OPTS)
    with pytest.raises(HoldoutError, match="resamples"):
        run_holdout(repo.root, variants={"c": FLAT}, resamples=0)
    with pytest.raises(HoldoutError, match="is taken"):
        run_holdout(repo.root, variants={"popularity": FLAT})
    with pytest.raises(HoldoutError, match="must not be empty"):
        run_holdout(repo.root, variants={"a b": FLAT})
    with pytest.raises(HoldoutError, match="max_files"):
        run_holdout(repo.root, max_files=1, **OPTS)
    with pytest.raises(HoldoutError, match="max_files"):
        run_holdout(repo.root, max_files=10 ** 9, **OPTS)
    for rev in ("nosuchrev", "--all", "HEAD~999"):
        with pytest.raises(HoldoutError, match="--rev .* does not name a commit"):
            run_holdout(repo.root, rev=rev, **OPTS)
    with pytest.raises(HoldoutError, match="not inside a git repository"):
        run_holdout(tmp_path / "nowhere-at-all", **OPTS)
    # A cut on a side branch is not on the mainline being scored.
    repo.git("checkout", "-q", "-b", "side", "HEAD~3")
    side = touch(repo, 30, "src/a.py")
    repo.git("checkout", "-q", "main")
    with pytest.raises(HoldoutError, match="first-parent line"):
        run_holdout(repo.root, cut=side, **OPTS)


@requires_git
def test_empty_and_tiny_histories_report_nothing_without_an_error(git_repo):
    empty = run_holdout(git_repo.root, **OPTS)
    assert empty["population"]["commits"] == 0 and empty["population"]["eligible"] == 0
    assert "nothing to report" in format_holdout(empty)
    touch(git_repo, 0, "a.py", "b.py")
    one = run_holdout(git_repo.root, **OPTS)
    assert one["population"] == dict(one["population"], commits=1, train_commits=0, eligible=0)
    assert one["methods"]["cochange"]["n"] == 0 and one["methods"]["cochange"]["mrr"] == 0.0


@requires_git
def test_report_is_a_function_of_the_history(repo, make_git_repo):
    one = run_holdout(repo.root, cut_share=0.5, per_commit=True, **OPTS)
    two = run_holdout(repo.root, cut_share=0.5, per_commit=True, **OPTS)
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert str(repo.root) not in json.dumps(one)
    assert set(one) == {"schema", "repo", "settings", "population", "train", "methods", "paired",
                        "per_commit"}
    assert one["settings"]["cut_share"] == 0.5 and one["settings"]["config"] == "defaults"


def test_parse_variants():
    base = CochangeSettings()
    out = parse_variants(["lift=measure:lift", "slow=half_life:0, min_support:2", "same"], base)
    assert out["lift"].measure == "lift" and out["lift"].half_life == base.half_life
    assert (out["slow"].half_life, out["slow"].min_support) == (0, 2)
    assert out["same"] == base
    assert parse_variants(["x=follow_renames:false"], base)["x"].follow_renames is False
    for bad in ("=measure:lift", "x=nope:1", "x=half_life:soon", "x=follow_renames:maybe",
                "x=measure"):
        with pytest.raises(HoldoutError):
            parse_variants([bad], base)
    with pytest.raises(GraphConfigError):
        parse_variants(["x=measure:pmi"], base)
    with pytest.raises(GraphConfigError):
        parse_variants(["x=max_files:100000"], base)


@requires_git
def test_main_text_json_and_errors(repo, tmp_path, capsys):
    graph = tmp_path / "g.tsv"
    graph.write_text("src/c.py\tlib/d.py\n")
    args = ["--repo", str(repo.root), "--cut", repo.commits["cut"], "--resamples", "20",
            "--no-config", "--cochange", "flat=half_life:0,min_score:0",
            "--cochange", "lift=measure:lift", "--graph", f"ext={graph}"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("Holdout  head=") and "scored: 5 commits, 10 seed queries" in out
    assert "flat vs popularity" in out and "graph:ext vs flat" in out
    assert main(args + ["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["methods"]) == {"flat", "lift", "graph:ext", "popularity", "directory"}
    target = tmp_path / "out" / "report.json"
    target.parent.mkdir()
    assert main(args + ["--json", str(target)]) == 0
    assert json.loads(target.read_text())["schema"] == 1
    assert capsys.readouterr().out.startswith("Holdout")
    for bad in (["--graph", "nopath"], ["--graph", f"x={tmp_path / 'absent'}"],
                ["--cochange", "x=measure:pmi"], ["--k", "a,b"], ["--cut", "nope"]):
        assert main(["--repo", str(repo.root), "--no-config", *bad]) == 1
        assert capsys.readouterr().err.startswith("Error:")
    assert main(["--repo", str(tmp_path / "missing")]) == 1


@requires_git
def test_main_never_writes_the_report_through_a_link(repo, tmp_path, capsys):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    link = tmp_path / "report.json"
    link.symlink_to(victim)
    assert main(["--repo", str(repo.root), "--no-config", "--resamples", "20",
                 "--json", str(link)]) == 0
    capsys.readouterr()
    assert victim.read_text() == "keep me\n"
    assert not link.is_symlink() and json.loads(link.read_text())["schema"] == 1


@requires_git
def test_a_mistyped_rev_is_an_error_not_an_empty_report(repo, capsys):
    assert main(["--repo", str(repo.root), "--rev", "nosuchrev", "--no-config"]) != 0
    captured = capsys.readouterr()
    assert "does not name a commit" in captured.err and "Population" not in captured.out


@requires_git
def test_a_damaged_branch_is_an_error_not_an_empty_history(repo):
    ref = repo.root / ".git" / "refs" / "heads" / "main"
    if not ref.is_file():
        pytest.skip("this git does not keep the branch as a loose ref")
    ref.write_bytes(b"1" * 40 + b"\n")
    with pytest.raises(HoldoutError, match="does not name a commit"):
        run_holdout(repo.root, **OPTS)


@requires_git
def test_a_deleted_file_is_neither_seed_nor_answer(git_repo):
    r = git_repo
    touch(r, 0, "a.py", "b.py", "c.py")
    for day in (1, 2, 3):
        touch(r, day, "a.py", "b.py")
    cut = touch(r, 4, "c.py")
    r.write("a.py", "edited\n")                 # the miner would count no pair for this commit
    r.rm("b.py")
    r.commit("edit a, delete b", 5)
    report = run_holdout(r.root, cut=cut, **OPTS)
    pop = report["population"]
    assert (pop["eligible"], pop["queries"]) == (0, 0)
    assert pop["skipped"] == {"single_file": 1, "bulk": 0, "unknown_files": 0}
    # With a third file changed in the same commit the two survivors are scored, b is not.
    r.git("reset", "-q", "--hard", cut)
    r.write("a.py", "edited\n")
    r.write("c.py", "edited\n")
    r.rm("b.py")
    r.commit("edit a and c, delete b", 5)
    report = run_holdout(r.root, cut=cut, per_commit=True, **OPTS)
    assert report["population"]["queries"] == 2 and report["per_commit"][0]["seeds"] == 2
    assert report["methods"]["cochange"]["mrr"] == 0.0


@requires_git
def test_a_bulk_commit_is_never_collected_and_the_report_does_not_change(repo, monkeypatch):
    from carta.graph import cochange as cochange_mod
    plain = run_holdout(repo.root, cut=repo.commits["cut"], **OPTS)
    assert plain["population"]["files_at_cut"] == 4
    for i in range(60):
        repo.write(f"gen/f{i}.txt", "x\n")
    touch(repo, 20, "src/a.py", "src/b.py")
    widths = []
    real = cochange_mod.iter_log_entries

    def spy(top, rows, **kwargs):
        for entry in real(top, rows, **kwargs):
            widths.append((len(entry.changes), entry.n_changes, entry.truncated))
            yield entry

    monkeypatch.setattr(cochange_mod, "iter_log_entries", spy)
    report = run_holdout(repo.root, cut=repo.commits["cut"], **OPTS)
    assert report["population"]["skipped"]["bulk"] == 1
    assert report["population"]["eligible"] == plain["population"]["eligible"]
    assert report["methods"] == plain["methods"]
    assert (0, 62, True) in widths and max(width for width, _n, _t in widths) <= 30
