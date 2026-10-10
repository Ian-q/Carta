"""Co-change in the replay (no leakage), fused with mentions, and shown by ``carta impact``."""
from __future__ import annotations

import dataclasses
import json

import pytest

from carta import cli
from carta.graph.build import build_graph
from carta.graph.eval import replay as replay_mod
from carta.graph.eval.cochange_arm import CochangeProvider
from carta.graph.eval.replay import PROVIDERS, ReplayError, _parse_arms, run_replay
from carta.graph.gitio import first_parent_log
from carta.graph.model import Edge
from carta.graph.query import (
    COMBINE_MODES, RRF_K, ImpactIndex, format_impact, rank_impact, run_impact,
)
from carta.graph.settings import DEFAULT_SETTINGS, CochangeSettings, graph_settings
from carta.graph.store import EdgeStore, store_path
from carta.graph.tests.conftest import requires_git

OPTS = dict(use_disk_cache=False, resamples=50, min_population=1)
FLAT = dataclasses.replace(
    DEFAULT_SETTINGS, cochange=CochangeSettings(half_life=0, min_score=0.0))


def m(doc: str, dst: str, line: int = 1, weight: float = 1.0) -> Edge:
    return Edge("mention", doc, dst, "file", line, evidence=f"names {dst}", weight=weight)


def co(src: str, doc: str, weight: float, text: str = "changed together in 3 of 4 commits"
       ) -> Edge:
    return Edge("cochange", src, doc, "doc", weight=weight, evidence=text, src_type="file",
                extractor="cochange", extractor_version=1)


def rank(edges, paths, **kw):
    return rank_impact(ImpactIndex(edges, 10), paths, top=None, **kw)


# -- fusing kinds ------------------------------------------------------------------

def swamped():
    """A mention ranking with large scores that is wrong, a history ranking that is right."""
    edges = [m("docs/wrong.md", "src/a.py", line) for line in (1, 2, 3)]
    edges += [m("docs/right.md", "src/a.py")]
    edges += [co("src/a.py", "docs/right.md", 0.9), co("src/a.py", "docs/other.md", 0.2)]
    return edges


def test_max_and_sum_let_the_kind_with_bigger_numbers_win():
    for mode in ("max", "sum"):
        out = rank(swamped(), ["src/a.py"], kinds=("mention", "cochange"), combine=mode)
        assert [r["doc"] for r in out][0] == "docs/wrong.md", mode


def test_rank_fusion_does_not_depend_on_the_units_of_a_score():
    out = rank(swamped(), ["src/a.py"], kinds=("mention", "cochange"), combine="rrf")
    assert [r["doc"] for r in out] == ["docs/right.md", "docs/wrong.md", "docs/other.md"]
    top = out[0]
    assert top["by_kind"] == {"cochange": {"rank": 1, "score": pytest.approx(0.9)},
                              "mention": {"rank": 2, "score": top["by_kind"]["mention"]["score"]}}
    assert top["score"] == pytest.approx(1.0 + (RRF_K + 1) / (RRF_K + 2))
    assert sorted(x["kind"] for x in top["matches"]) == ["cochange", "mention"]
    # Scaling every mention weight changes nothing: only ranks are read.
    scaled = [dataclasses.replace(e, weight=e.weight * 0.01) if e.kind == "mention" else e
              for e in swamped()]
    again = rank(scaled, ["src/a.py"], kinds=("mention", "cochange"), combine="rrf")
    assert [(r["doc"], r["score"]) for r in again] == [(r["doc"], r["score"]) for r in out]


def test_rank_fusion_weights_scale_a_kind():
    kinds = ("mention", "cochange")
    even = rank(swamped(), ["src/a.py"], kinds=kinds, combine="rrf",
                weights={"mention": 1.0, "cochange": 1.0})
    loud = rank(swamped(), ["src/a.py"], kinds=kinds, combine="rrf",
                weights={"mention": 1.0, "cochange": 0.01})
    assert even[0]["doc"] == "docs/right.md" and loud[0]["doc"] == "docs/wrong.md"


def test_norm_adds_each_score_as_a_share_of_its_kinds_best():
    out = rank(swamped(), ["src/a.py"], kinds=("mention", "cochange"), combine="norm")
    by_doc = {r["doc"]: r for r in out}
    assert by_doc["docs/wrong.md"]["score"] == pytest.approx(1.0)
    assert by_doc["docs/other.md"]["score"] == pytest.approx(0.2 / 0.9)
    assert out[0]["doc"] == "docs/right.md" and out[0]["score"] > 1.0


@pytest.mark.parametrize("mode", [mode for mode in COMBINE_MODES if mode != "max"])
def test_fusing_with_a_kind_that_found_nothing_is_the_identity(mode):
    edges = [m("docs/a.md", "src/a.py"), m("docs/b.md", "src/a.py", 2), m("docs/b.md", "src/a.py")]
    alone = rank(edges, ["src/a.py"], kinds=("mention",))
    fused = rank(edges, ["src/a.py"], kinds=("mention", "cochange"), combine=mode)
    assert fused == alone
    assert rank([], ["src/a.py"], kinds=("mention", "cochange"), combine=mode) == []
    cut = rank_impact(ImpactIndex(edges, 10), ["src/a.py"], kinds=("mention", "cochange"),
                      combine=mode, top=1, min_score=0.5)
    assert [r["doc"] for r in cut] == ["docs/b.md"]


def test_fused_ranking_honours_top_min_score_and_excluded_docs():
    kinds = ("mention", "cochange")
    full = rank(swamped(), ["src/a.py"], kinds=kinds, combine="rrf")
    index = ImpactIndex(swamped(), 10)
    assert len(rank_impact(index, ["src/a.py"], kinds=kinds, combine="rrf", top=2)) == 2
    high = rank_impact(index, ["src/a.py"], kinds=kinds, combine="rrf", top=None, min_score=1.5)
    assert [r["doc"] for r in high] == ["docs/right.md"]
    without = rank_impact(index, ["src/a.py"], kinds=kinds, combine="rrf", top=None,
                          exclude_docs=["docs/right.md"])
    # Excluded before ranking, as if never there: each remaining doc is now first
    # in its own kind, and the tie goes to the smaller path.
    assert [r["doc"] for r in full] == ["docs/right.md", "docs/wrong.md", "docs/other.md"]
    assert [(r["rank"], r["doc"]) for r in without] == [(1, "docs/other.md"),
                                                        (2, "docs/wrong.md")]
    with pytest.raises(ValueError):
        rank(swamped(), ["src/a.py"], kinds=kinds, combine="average")


# -- two providers of known quality, through the replay ------------------------------

class _Toy(replay_mod.EdgeProvider):
    """Ranks the docs of ``answers[query file]`` in the order given, with given weights."""

    def __init__(self, kind, answers):
        self.kind, self.answers = kind, answers

    def edges_at(self, snapshot):
        docs = set(snapshot.docs)
        return [Edge(self.kind, src, doc, "doc", weight=w, src_type="file", extractor=self.kind)
                for src, ranked in sorted(self.answers.items())
                for doc, w in ranked if doc in docs]


@pytest.fixture
def two_docs_repo(git_repo):
    """Six commits that each edit src/a.py with docs/right.md; docs/wrong.md never moves."""
    repo = git_repo
    repo.write("docs/right.md", "r\n")
    repo.write("docs/wrong.md", "w\n")
    repo.write("docs/noise.md", "n\n")
    repo.write("src/a.py", "0\n")
    repo.commit("root", 0)
    for day in range(1, 7):
        repo.write("src/a.py", f"{day}\n")
        repo.write("docs/right.md", f"r{day}\n")
        repo.commit(f"day {day}", day)
    return repo


@requires_git
def test_a_good_provider_and_a_loud_bad_one(two_docs_repo, monkeypatch):
    """The test the spec asked for before any ``both`` arm is read."""
    good = {"src/a.py": [("docs/right.md", 0.9), ("docs/noise.md", 0.1)]}
    loud = {"src/a.py": [("docs/wrong.md", 100.0), ("docs/right.md", 10.0)]}
    monkeypatch.setitem(PROVIDERS, "symbol", lambda ctx: _Toy("symbol", good))
    monkeypatch.setitem(PROVIDERS, "related", lambda ctx: _Toy("related", loud))
    arms = {"good": ["symbol"], "loud": ["related"]}
    arms.update({mode: ["symbol", "related", "@" + mode] for mode in COMBINE_MODES})
    settings = dataclasses.replace(FLAT, impact_weights=(("related", 1.0), ("symbol", 1.0)))
    report = run_replay(two_docs_repo.root, arms=arms, settings=settings, hub_share=1.0, **OPTS)
    mrr = {arm: report["methods"][f"graph[{arm}]"]["mrr"] for arm in arms}
    assert report["population"]["eligible"] == 6
    assert mrr["good"] == 1.0 and mrr["loud"] == 0.5
    # Adding raw scores, or taking the larger, hands the answer to the loud provider.
    assert mrr["sum"] == 0.5 and mrr["max"] == 0.5
    # Fusing ranks keeps the doc both providers agree on first.
    assert mrr["rrf"] == 1.0 and mrr["norm"] == 1.0
    assert report["settings"]["combine"] == {mode: mode for mode in COMBINE_MODES}


def test_arm_combine_mode_is_parsed_and_validated(two_docs_repo):
    assert _parse_arms(["both=mention+cochange@rrf", "m=mention"]) == {
        "both": ["mention", "cochange", "@rrf"], "m": ["mention"]}
    with pytest.raises(ReplayError, match="combine"):
        run_replay(two_docs_repo.root, arms={"x": ["mention", "cochange", "@avg"]}, **OPTS)
    report = run_replay(two_docs_repo.root, arms={"x": ["mention", "cochange"]}, **OPTS)
    assert report["settings"]["combine"] == {"x": DEFAULT_SETTINGS.impact_combine}
    assert DEFAULT_SETTINGS.impact_combine == "norm"
    assert report["settings"]["cochange"]["measure"] == "confidence"
    assert run_replay(two_docs_repo.root, **OPTS)["settings"]["cochange"] == {}


# -- the co-change arm: nothing from the commit under test, or after it ----------------

@pytest.fixture
def leak_repo(git_repo):
    """src/a.py and docs/a.md first change together in ``first``; again in ``second``."""
    repo, c = git_repo, {}
    repo.write("docs/a.md", "a\n")
    repo.write("docs/b.md", "b\n")
    repo.write("src/a.py", "0\n")
    repo.write("src/b.py", "0\n")
    c["root"] = repo.commit("root", 0)
    repo.write("src/b.py", "1\n")
    repo.write("docs/b.md", "b1\n")
    c["warm"] = repo.commit("b with its doc", 1)
    repo.write("src/a.py", "1\n")
    repo.write("docs/a.md", "a1\n")
    c["first"] = repo.commit("a with its doc, for the first time", 2)
    repo.write("src/a.py", "2\n")
    repo.write("docs/a.md", "a2\n")
    c["second"] = repo.commit("a with its doc again", 3)
    repo.write("src/a.py", "3\n")
    repo.write("docs/b.md", "b3\n")
    c["cross"] = repo.commit("a with the other doc, never seen before", 4)
    return repo, c


@requires_git
def test_a_commit_cannot_see_itself_or_its_future(leak_repo):
    repo, c = leak_repo
    report = run_replay(repo.root, arms={"cochange": ["cochange"]}, settings=FLAT,
                        per_commit=True, hub_share=1.0, **OPTS)
    ranks = {row["commit"]: row["ranks"]["graph[cochange]"] for row in report["per_commit"]}
    # The only evidence for (src/a.py, docs/a.md) at ``first`` is ``first`` itself and
    # the commits after it. Seeing either would rank the doc; it must not be ranked.
    assert ranks[c["first"]] is None
    # One commit later that evidence is history, and is used.
    assert ranks[c["second"]] == 1
    # ``cross`` pairs src/a.py with docs/b.md for the first time: again nothing to find.
    assert ranks[c["cross"]] is None
    assert ranks[c["warm"]] is None
    assert set(ranks) == {c["warm"], c["first"], c["second"], c["cross"]}


@requires_git
def test_the_table_holds_exactly_the_commits_before_the_one_being_ranked(leak_repo, monkeypatch):
    repo, c = leak_repo
    log = [entry.sha for entry in first_parent_log(repo.root, "HEAD")]
    seen = []

    class Spy(CochangeProvider):
        def relevant_edges(self, snapshot, paths):
            seen.append((snapshot.parent, self.table.seq, list(self.observed)))
            return super().relevant_edges(snapshot, paths)

    monkeypatch.setitem(PROVIDERS, "cochange", Spy)
    run_replay(repo.root, arms={"cochange": ["cochange"]}, settings=FLAT, **OPTS)
    assert len(seen) == 4
    for parent, seq, observed in seen:
        position = log.index(parent)
        assert observed == log[:position + 1] and seq == position + 1
    # Without the reduced edge sets the same holds through edges_at.
    full = run_replay(repo.root, arms={"cochange": ["cochange"]}, settings=FLAT,
                      incremental=False, **OPTS)
    lean = run_replay(repo.root, arms={"cochange": ["cochange"]}, settings=FLAT, **OPTS)
    assert full == lean


@requires_git
def test_poisoning_the_future_does_not_change_an_earlier_commits_row(make_git_repo):
    """Two histories that agree up to a commit give that commit the same ranking."""
    def base(repo):
        repo.write("docs/a.md", "a\n")
        repo.write("docs/z.md", "z\n")
        repo.write("src/a.py", "0\n")
        repo.commit("root", 0)
        repo.write("src/a.py", "1\n")
        repo.write("docs/a.md", "a1\n")
        repo.commit("pair", 1)
        repo.write("src/a.py", "2\n")
        repo.write("docs/a.md", "a2\n")
        return repo.commit("under test", 2)

    short, long = make_git_repo("short"), make_git_repo("long")
    sha = base(short)
    assert base(long) == sha
    for day in range(3, 9):
        long.write("src/a.py", f"{day}\n")
        long.write("docs/z.md", f"z{day}\n")
        long.commit(f"later {day}", day)

    def row(repo):
        report = run_replay(repo.root, arms={"cochange": ["cochange"]}, settings=FLAT,
                            range_spec=sha, per_commit=True, hub_share=1.0, **OPTS)
        return [r for r in report["per_commit"] if r["commit"] == sha][0]

    assert row(short) == row(long)
    assert row(long)["ranks"]["graph[cochange]"] == 1


@requires_git
def test_the_replay_ends_on_the_edges_a_build_stores(leak_repo, monkeypatch):
    """One set of rules: the table carried through a replay equals the built store."""
    repo, _c = leak_repo
    made = []

    def factory(context):
        made.append(CochangeProvider(context))
        return made[-1]

    monkeypatch.setitem(PROVIDERS, "cochange", factory)
    settings = graph_settings({})
    run_replay(repo.root, arms={"cochange": ["cochange"]}, settings=settings, **OPTS)
    build_graph(repo.root, {})
    with EdgeStore(store_path(repo.root)) as store:
        stored = list(store.iter_edges(kinds=("cochange",)))
    table = made[0].table
    mined = sorted(e for path in table.files
                   for e in table.edges_from(path, lambda p: p.endswith(".md")))
    assert stored == mined and len(stored) >= 4


@requires_git
def test_union_arm_runs_with_the_real_providers(leak_repo):
    repo, c = leak_repo
    repo.write("docs/a.md", "The code is `src/a.py`.\n")
    repo.commit("doc names the file", 5)
    repo.write("src/a.py", "9\n")
    repo.write("docs/a.md", "The code is `src/a.py`. Still.\n")
    last = repo.commit("both again", 6)
    arms = {"mention": ["mention"], "cochange": ["cochange"],
            "both": ["mention", "cochange", "@rrf"]}
    report = run_replay(repo.root, arms=arms, settings=FLAT, per_commit=True, hub_share=1.0,
                        **OPTS)
    row = [r for r in report["per_commit"] if r["commit"] == last][0]["ranks"]
    assert row["graph[mention]"] == 1 and row["graph[cochange]"] == 1 and row["graph[both]"] == 1
    assert set(report["paired"]["graph[both]"]) >= {"graph[mention]", "graph[cochange]"}


# -- carta impact ------------------------------------------------------------------

CONFIG = ("project_name: demo\nqdrant_url: http://127.0.0.1:9\n"
          "embed:\n  ollama_url: http://127.0.0.1:9\n")


@pytest.fixture
def project(git_repo, monkeypatch):
    """docs/named.md names src/a.py and never changes; docs/moves.md changes with it."""
    repo = git_repo
    repo.write(".carta/config.yaml", CONFIG)
    repo.write(".gitignore", ".carta/\n")
    repo.write("docs/named.md", "The parser is `src/a.py`.\n")
    repo.write("docs/moves.md", "Release notes.\n")
    repo.write("src/a.py", "0\n")
    repo.commit("root", 0)
    for day in range(1, 4):
        repo.write("src/a.py", f"{day}\n")
        repo.write("docs/moves.md", f"Release notes {day}.\n")
        repo.commit(f"day {day}", day)
    repo.write("src/a.py", "4\n")
    repo.commit("code only", 4)
    monkeypatch.chdir(repo.root)
    return repo


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["carta", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@requires_git
def test_impact_labels_what_a_doc_says_apart_from_what_history_shows(project):
    out = run_impact(project.root, {}, paths=["src/a.py"], kinds=["mention", "cochange"])
    by_doc = {r["doc"]: r for r in out["results"]}
    assert set(by_doc) == {"docs/named.md", "docs/moves.md"}
    named = by_doc["docs/named.md"]["matches"]
    assert [(x["kind"], x["match"]) for x in named] == [("mention", "exact")]
    assert named[0]["evidence"] == [{"line": 1, "text": "The parser is `src/a.py`."}]
    moved = by_doc["docs/moves.md"]["matches"]
    assert [(x["kind"], x["match"], x["target_type"]) for x in moved] == [
        ("cochange", "cochange", "file")]
    assert moved[0]["evidence"] == [{"line": 0, "text": "changed together in 3 of 4 commits"}]
    assert out["query"]["kinds"] == ["cochange", "mention"] and out["unmatched_paths"] == []
    text = format_impact(out)
    assert "2 docs are tied to 1 path" in text
    assert "correlation, not a claim that the doc describes it" in text
    assert ("src/a.py  [history] changed together in 3 of 4 commits "
            "(git history; the doc need not name it)") in text
    assert "src/a.py  [named] exact, named by 1 doc" in text
    assert "L1: The parser is `src/a.py`." in text
    assert "L0" not in text


@requires_git
def test_history_alone_and_the_default_kinds(project):
    only = run_impact(project.root, {}, paths=["src/a.py"], kinds=["cochange"])
    assert [r["doc"] for r in only["results"]] == ["docs/moves.md"]
    # 3 of 4 commits, a little less because the default half-life discounts older ones
    assert only["results"][0]["score"] == pytest.approx(0.75, abs=0.01)
    text = format_impact(only)
    assert "1 doc is tied to 1 path" in text and "[history]" in text and "[named]" not in text
    default = run_impact(project.root, {}, paths=["src/a.py"])
    assert default["query"]["kinds"] == ["cochange", "mention"]        # both, by default
    assert {r["doc"] for r in default["results"]} == {"docs/named.md", "docs/moves.md"}
    named = run_impact(project.root, {}, paths=["src/a.py"], kinds=["mention"])
    assert [r["doc"] for r in named["results"]] == ["docs/named.md"]
    text = format_impact(named)
    assert "1 doc mentions 1 path" in text and "[named]" not in text and "history" not in text
    nothing = format_impact(run_impact(project.root, {}, paths=["docs/zzz.py"],
                                       kinds=["mention", "cochange"]))
    # With nothing found there is nothing to label, and the wording is the plain one.
    assert "No doc mentions the 1 queried path" in nothing
    assert "No doc mentions: docs/zzz.py" in nothing
    some = format_impact(run_impact(project.root, {}, paths=["src/a.py", "docs/zzz.py"]))
    assert "No doc names or has changed with: docs/zzz.py" in some


@requires_git
def test_impact_mines_history_only_when_a_history_kind_is_asked_for(project):
    run_impact(project.root, {}, paths=["src/a.py"], kinds=["mention"])
    with EdgeStore(store_path(project.root)) as store:
        assert store.counts()["by_kind"].get("cochange") is None
    run_impact(project.root, {}, paths=["src/a.py"], kinds=["cochange"])
    with EdgeStore(store_path(project.root)) as store:
        assert store.counts()["by_kind"]["cochange"] == 2
    cfg = {"graph": {"impact": {"kinds": ["mention"]}}}
    assert run_impact(project.root, cfg, paths=["src/a.py"])["query"]["kinds"] == ["mention"]


@requires_git
def test_a_diff_is_not_evidence_about_itself(project):
    """The store is mined up to HEAD, which holds the very commits ``--diff`` asks about."""
    repo = project
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/a.py", "feature\n")
    repo.write("src/new.py", "new\n")
    repo.write("docs/fresh.md", "Written in this change.\n")
    repo.commit("the change being asked about", 9)
    out = run_impact(repo.root, {}, diff="main...HEAD", kinds=["cochange"])
    assert out["query"]["paths"] == ["src/a.py", "src/new.py"]
    by_doc = {r["doc"]: r for r in out["results"]}
    # docs/fresh.md met src/a.py only in the change itself: that is not history.
    assert "docs/fresh.md" not in by_doc
    # docs/moves.md had changed with src/a.py three times before: it stays.
    assert list(by_doc) == ["docs/moves.md"]
    assert [m["path"] for m in by_doc["docs/moves.md"]["matches"]] == ["src/a.py"]
    assert out["unmatched_paths"] == ["src/new.py"]
    # Asked as plain paths, with no range, the store is read as it is.
    plain = run_impact(repo.root, {}, paths=["src/new.py"], kinds=["cochange"])
    assert [r["doc"] for r in plain["results"]] == ["docs/fresh.md"]
    # Once the change is merged it is history like any other.
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--ff-only", "feature")
    repo.write("src/new.py", "newer\n")
    repo.commit("a later change", 10)
    later = run_impact(repo.root, {}, diff="HEAD~1..HEAD", kinds=["cochange"])
    assert [r["doc"] for r in later["results"]] == ["docs/fresh.md"]


@requires_git
def test_cli_build_reports_history_and_can_skip_it(project, monkeypatch, capsys):
    code, out, err = run_cli(monkeypatch, capsys, "graph", "build", "--no-cochange")
    assert (code, err) == (0, "") and "history" not in out
    code, out, _ = run_cli(monkeypatch, capsys, "graph", "status", "--json")
    status = json.loads(out)
    assert status["stale_reasons"] == ["cochange_behind"] and status["cochange"]["commits"] == 0
    code, out, err = run_cli(monkeypatch, capsys, "graph", "build")
    assert (code, err) == (0, "")
    assert "  history: 2 co-change edges from 4 commits (5 new, 0 skipped as bulk)" in out
    code, out, _ = run_cli(monkeypatch, capsys, "graph", "build", "--json")
    report = json.loads(out)
    assert report["cochange"]["status"] == "ok" and report["cochange"]["new_commits"] == 0
    assert report["by_kind"]["cochange"] == 2
    code, out, _ = run_cli(monkeypatch, capsys, "graph", "status")
    assert "up to date" in out and "history: co-change edges from 5 mainline commits" in out
    code, out, err = run_cli(monkeypatch, capsys, "impact", "src/a.py", "--kinds",
                             "mention,cochange")
    assert code == 0 and "[history]" in out and "[named]" in out


@requires_git
def test_cli_forwards_holdout(project, monkeypatch, capsys):
    code, out, err = run_cli(monkeypatch, capsys, "graph", "holdout", "--cut-share", "0.5",
                             "--resamples", "20", "--no-config")
    assert code == 0 and out.startswith("Holdout  head=") and "Population:" in out
    code, out, err = run_cli(monkeypatch, capsys, "graph", "holdout", "--cut", "nope")
    assert code == 1 and err.startswith("Error:")
    code, out, err = run_cli(monkeypatch, capsys, "graph")
    assert code == 1 and "holdout" in err
