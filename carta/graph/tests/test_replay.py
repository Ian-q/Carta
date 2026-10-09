"""The PR-replay eval, on a synthetic history with hand-counted answers."""
from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest

from carta.graph.eval import replay as replay_mod
from carta.graph.eval.replay import (
    BASELINES, PROVIDERS, ReplayError, classify_commit, format_replay, main, register_provider,
    run_replay,
)
from carta.graph.gitio import first_parent_log
from carta.graph.model import Edge
from carta.graph.settings import DEFAULT_SETTINGS, graph_settings
from carta.graph.store import store_path
from carta.graph.tests.conftest import GitRepo, requires_git

pytestmark = requires_git

GRAPH = "graph[mention]"
OPTS = dict(max_files=5, hub_share=0.4, use_disk_cache=False, resamples=200)


def build_history(repo) -> dict:
    """Fifteen mainline commits covering every case the replay must classify."""
    c = {}
    repo.write("README.md", "# Demo\n")
    repo.write("CHANGELOG.md", "# Changes\n")
    repo.write("docs/parser.md", "# Parser\n\nFraming lives in `src/parser/frame.py`.\n")
    repo.write("docs/store.md", "# Store\n\nEverything under `src/store/`.\n")
    repo.write("docs/misc.md", "# Misc\n\nNothing named here.\n")
    repo.write("src/parser/frame.py", "FRAME = 1\n" * 12)
    repo.write("src/store/db.py", "DB = 1\n")
    repo.write("src/store/cache.py", "CACHE = 1\n")
    repo.write("tools/gen.py", "GEN = 1\n")
    c["root"] = repo.commit("root", 0)

    repo.write("src/parser/frame.py", "FRAME = 2\n" * 12)
    repo.write("docs/parser.md", "# Parser\n\nFraming lives in `src/parser/frame.py`. v2\n")
    c["parser"] = repo.commit("parser and its doc", 1)

    repo.write("src/store/db.py", "DB = 2\n")
    repo.write("docs/store.md", "# Store\n\nEverything under `src/store/`. v2\n")
    c["store"] = repo.commit("store and its doc", 2)

    repo.write("src/parser/frame.py", "FRAME = 3\n" * 12)
    repo.write("docs/parser.md", "# Parser\n\nFraming lives in `src/parser/frame.py`. v3\n")
    repo.write("CHANGELOG.md", "# Changes\n- parser\n")
    c["parser_changelog"] = repo.commit("parser, doc and changelog", 3)

    repo.write("tools/gen.py", "GEN = 2\n")
    repo.write("CHANGELOG.md", "# Changes\n- parser\n- gen\n")
    c["changelog_only"] = repo.commit("gen with changelog only", 4)

    repo.write("src/store/cache.py", "CACHE = 2\n")
    c["source_only"] = repo.commit("source only", 5)

    repo.write("docs/misc.md", "# Misc\n\nStill nothing named here.\n")
    c["docs_only"] = repo.commit("docs only", 6)

    repo.write("src/new_mod.py", "NEW = 1\n")
    repo.write("docs/new.md", "# New\n\nSee `src/new_mod.py`.\n")
    c["new_doc"] = repo.commit("source with a brand new doc", 7)

    for i in range(6):
        repo.write(f"bulk/f{i}.txt", f"{i}\n")
    repo.write("CHANGELOG.md", "# Changes\n- parser\n- gen\n- bulk\n")
    c["bulk"] = repo.commit("bulk import", 8)

    repo.git("mv", "src/parser/frame.py", "src/parser/framing.py")
    repo.write("docs/parser.md", "# Parser\n\nFraming lives in `src/parser/framing.py`.\n")
    repo.write("CHANGELOG.md", "# Changes\n- parser\n- gen\n- bulk\n- rename\n")
    c["rename"] = repo.commit("rename frame to framing", 9)

    repo.git("checkout", "-q", "-b", "side")
    repo.write("src/store/db.py", "DB = 3\n")
    c["side_1"] = repo.commit("side: source", 10)
    repo.write("docs/store.md", "# Store\n\nEverything under `src/store/`. v3\n")
    c["side_2"] = repo.commit("side: doc", 11)
    repo.git("checkout", "-q", "main")
    repo.write("tools/gen.py", "GEN = 3\n")
    repo.write("CHANGELOG.md", "# Changes\n- parser\n- gen\n- bulk\n- rename\n- gen again\n")
    c["main_between"] = repo.commit("gen again with changelog", 12)
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side", date_index=13)
    c["merge"] = repo.git("rev-parse", "HEAD").strip()

    # The trap: the doc starts naming the file in the very commit that edits it.
    repo.write("tools/gen.py", "GEN = 4\n")
    repo.write("docs/misc.md", "# Misc\n\nThe generator is `tools/gen.py`.\n")
    c["trap"] = repo.commit("gen, and misc starts naming it", 14)

    c["empty"] = repo.commit("nothing at all", 15)

    repo.write("tools/gen.py", "GEN = 5\n")
    repo.write("docs/misc.md", "# Misc\n\nThe generator is `tools/gen.py`. v2\n")
    c["after_trap"] = repo.commit("gen and misc again", 16)
    return c


ELIGIBLE = ("parser", "store", "parser_changelog", "changelog_only", "rename", "main_between",
            "merge", "trap", "after_trap")


@pytest.fixture(scope="module")
def _built(tmp_path_factory):
    """The history, built once; each test works on its own copy."""
    base = tmp_path_factory.mktemp("replay-history")
    empty = base / "empty.gitconfig"
    empty.write_text("")
    repo = GitRepo(base / "repo", empty)
    return repo, build_history(repo), empty


@pytest.fixture
def history(_built, tmp_path):
    built, commits, empty = _built
    shutil.copytree(built.root, tmp_path / "repo", symlinks=True)
    repo = GitRepo.__new__(GitRepo)
    repo.root = tmp_path / "repo"
    repo._env = dict(built._env)
    return repo, commits


def rows(report: dict) -> dict:
    return {row["commit"]: row for row in report["per_commit"]}


# -- classification ----------------------------------------------------------

def test_classify_commit_each_skip_reason_in_priority_order():
    s = DEFAULT_SETTINGS
    src, doc, new = ("M", "src/a.py"), ("M", "docs/a.md"), ("A", "docs/new.md")
    many = [("A", f"gen/f{i}.txt") for i in range(6)]
    table = [
        ([src, doc], False, "root_commit"),
        ([], True, "empty"),
        (many + [doc], True, "bulk"),
        ([doc, new], True, "docs_only"),
        ([src], True, "no_docs_edited"),
        ([src, new], True, "only_new_docs"),
        ([src, doc], True, None),
        ([src, ("D", "docs/a.md")], True, None),
        # priority: a root commit that is also empty, bulk or docs-only is a root commit
        ([], False, "root_commit"),
        (many, False, "root_commit"),
        # bulk wins over docs_only and no_docs_edited
        ([("M", f"docs/d{i}.md") for i in range(6)], True, "bulk"),
        (many, True, "bulk"),
    ]
    for changes, has_parent, expected in table:
        assert classify_commit(changes, has_parent, s, 5).reason == expected, changes
    got = classify_commit([src, doc, new, ("D", "docs/old.md")], True, s, 5)
    assert got.reason is None
    assert got.query == ("src/a.py",)
    assert got.gold == ("docs/a.md", "docs/old.md")
    assert got.new_docs == 1 and got.edited_docs == ("docs/a.md", "docs/new.md", "docs/old.md")


def test_classify_exclusion_applies_to_the_doc_side_only():
    s = graph_settings({"excluded_paths": ["vendor/"]})
    got = classify_commit(
        [("M", "tools/build/gen.py"), ("M", "vendor/lib/README.md"), ("M", "vendor/lib/x.c"),
         ("M", "docs/a.md")], True, s, 100)
    assert got.reason is None
    # build/ is excluded by default and vendor/ by config, but both stay in the query
    assert got.query == ("tools/build/gen.py", "vendor/lib/x.c")
    assert got.gold == ("docs/a.md",) and got.excluded_docs == 1
    only = classify_commit([("M", "src/a.py"), ("M", "vendor/lib/README.md")], True, s, 100)
    assert only.reason == "no_docs_edited"


# -- population and scoring -----------------------------------------------------

def test_population_counts_add_up(history):
    repo, c = history
    report = run_replay(repo.root, per_commit=True, **OPTS)
    pop = report["population"]
    assert pop["commits_scanned"] == 15
    assert pop["eligible"] == 9
    assert pop["skipped"] == {"root_commit": 1, "empty": 1, "bulk": 1, "docs_only": 1,
                              "no_docs_edited": 1, "only_new_docs": 1}
    assert pop["commits_scanned"] == pop["eligible"] + sum(pop["skipped"].values())
    assert pop["new_docs_not_gold"] == 1
    assert pop["docs_at_end"] == 6
    assert pop["gold_docs_mean"] == pytest.approx(11 / 9, abs=1e-6)
    assert pop["gold_docs_median"] == 1
    assert [row["commit"] for row in report["per_commit"]] == [c[name] for name in ELIGIBLE]
    assert report["repo"] == {"head": c["after_trap"], "range": "all"}


def test_graph_arm_finds_the_named_doc(history):
    repo, c = history
    report = run_replay(repo.root, per_commit=True, **OPTS)
    by = rows(report)
    for name in ("parser", "store", "parser_changelog", "rename", "merge", "after_trap"):
        assert by[c[name]]["ranks"][GRAPH] == 1, name
    for name in ("changelog_only", "main_between"):      # nothing names tools/gen.py yet
        assert by[c[name]]["ranks"][GRAPH] is None, name
    graph = report["methods"][GRAPH]
    assert graph["n"] == 9
    assert graph["any_hit_at"]["1"] == pytest.approx(6 / 9, abs=1e-6)
    assert graph["mrr"] == pytest.approx(6 / 9, abs=1e-6)
    # recall: 4 single-gold hits + 2 commits with one of two gold docs found
    assert graph["recall_at"]["5"] == pytest.approx((4 + 0.5 + 0.5) / 9, abs=1e-6)
    assert graph["precision_at"]["1"] == pytest.approx(6 / 9, abs=1e-6)


def test_baselines_hand_checked_on_the_first_eligible_commit(history):
    repo, c = history
    by = rows(run_replay(repo.root, per_commit=True, **OPTS))
    first = by[c["parser"]]
    assert first["gold"] == ["docs/parser.md"] and first["n_query"] == 1
    # Before it only the root commit exists, which added all five docs once:
    # popularity ties break by path, and docs/parser.md is the fourth.
    assert first["ranks"]["popularity"] == 4
    assert first["ranks"]["basename"] == 1 and first["ranks"]["basename_idf"] == 1


def test_no_leakage_from_the_commit_itself(history):
    repo, c = history
    report = run_replay(repo.root, per_commit=True, **OPTS)
    by = rows(report)
    trap = by[c["trap"]]
    assert trap["gold"] == ["docs/misc.md"]
    # docs/misc.md names tools/gen.py only from this commit on; at the parent it names nothing
    assert trap["ranks"][GRAPH] is None
    assert trap["ranks"]["basename"] is None and trap["ranks"]["basename_idf"] is None
    assert by[c["after_trap"]]["ranks"][GRAPH] == 1         # visible once it is in the parent

    clean = json.dumps(report, sort_keys=True)
    # A dirty working tree and index that name every source file must change nothing.
    repo.write("docs/misc.md", "`tools/gen.py` `src/store/db.py` `src/parser/framing.py`\n")
    repo.write("docs/leak.md", "`tools/gen.py` `src/store/db.py` `src/new_mod.py`\n")
    repo.git("add", "-A")
    repo.write("README.md", "`tools/gen.py`\n")
    dirty = run_replay(repo.root, per_commit=True, **OPTS)
    assert json.dumps(dirty, sort_keys=True) == clean


def test_never_reads_worktree_or_store(history):
    repo, _c = history
    clean = json.dumps(run_replay(repo.root, per_commit=True, **OPTS), sort_keys=True)
    for path in sorted(repo.root.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(repo.root).parts:
            path.unlink()
    again = run_replay(repo.root, per_commit=True, **OPTS)
    assert json.dumps(again, sort_keys=True) == clean
    assert not store_path(repo.root).exists()
    assert not (repo.root / ".carta").exists()


def test_created_docs_are_not_gold_and_are_counted(history):
    repo, c = history
    report = run_replay(repo.root, per_commit=True, **OPTS)
    assert c["new_doc"] not in rows(report)
    assert report["population"]["skipped"]["only_new_docs"] == 1
    assert report["population"]["new_docs_not_gold"] == 1


def test_merge_commit_is_scored_against_first_parent(history):
    repo, c = history
    by = rows(run_replay(repo.root, per_commit=True, **OPTS))
    assert c["side_1"] not in by and c["side_2"] not in by
    merge = by[c["merge"]]
    assert merge["gold"] == ["docs/store.md"] and merge["n_query"] == 1
    assert merge["ranks"][GRAPH] == 1


def test_rename_is_delete_plus_add(history):
    repo, c = history
    rename = rows(run_replay(repo.root, per_commit=True, **OPTS))[c["rename"]]
    assert rename["n_query"] == 2                   # the old path and the new one
    assert rename["gold"] == ["CHANGELOG.md", "docs/parser.md"]
    assert rename["ranks"][GRAPH] == 1              # found through the old name, at the parent


def test_baselines_scored_on_identical_population(history):
    repo, _c = history
    report = run_replay(repo.root, **OPTS)
    assert set(report["methods"]) == {GRAPH, *BASELINES}
    assert {m["n"] for m in report["methods"].values()} == {9}
    assert set(report["no_hub"]["methods"]) == {GRAPH, *BASELINES}
    assert {m["n"] for m in report["no_hub"]["methods"].values()} == {7}
    assert set(report["paired"][GRAPH]) == set(BASELINES)
    pair = report["paired"][GRAPH]["popularity"]["mrr"]
    assert pair["better"] + pair["worse"] + pair["tied"] == 9
    assert pair["ci95"][0] <= pair["delta"] <= pair["ci95"][1]


def test_hub_variant_removes_hub_from_gold_and_rankings_and_shrinks_population(history):
    repo, c = history
    report = run_replay(repo.root, per_commit=True, **OPTS)
    no_hub = report["no_hub"]
    assert no_hub["hub_docs"] == [{"doc": "CHANGELOG.md", "commits": 4, "share": 0.444444}]
    assert no_hub["population"] == {"eligible": 7, "dropped": 2, "conclusive": False}
    # With the changelog out of gold the graph arm misses only the trap.
    assert no_hub["methods"][GRAPH]["any_hit_at"]["1"] == pytest.approx(6 / 7, abs=1e-6)
    assert no_hub["methods"][GRAPH]["recall_at"]["5"] == pytest.approx(6 / 7, abs=1e-6)
    # Popularity ranks the changelog first almost everywhere; removing it before
    # truncation moves every other doc up one place, so its no-hub ranks improve.
    by = rows(report)
    last = by[c["after_trap"]]
    assert last["ranks"]["popularity"] > last["no_hub_ranks"]["popularity"]
    assert by[c["changelog_only"]]["no_hub_ranks"] is None      # left the variant's population
    # A lower threshold makes more docs hubs; a threshold of 1 makes none.
    none = run_replay(repo.root, **dict(OPTS, hub_share=1.0))
    assert none["no_hub"]["hub_docs"] == []
    assert none["no_hub"]["methods"] == none["methods"]


def test_alarm_section_counts_source_only_commits(history):
    repo, _c = history
    alarm = run_replay(repo.root, **OPTS)["alarm"]
    assert alarm["commits"] == 1
    # src/store/cache.py: docs/store.md names the directory, so the graph arm speaks up
    assert alarm["methods"][GRAPH] == {"share_any": 1.0, "mean_returned_at_5": 1.0}
    assert alarm["methods"]["basename"] == {"share_any": 0.0, "mean_returned_at_5": 0.0}
    assert alarm["methods"]["popularity"]["share_any"] == 1.0
    assert alarm["methods"]["popularity"]["mean_returned_at_5"] == 5.0


def test_minimum_population_marks_the_result_inconclusive(history):
    repo, _c = history
    report = run_replay(repo.root, **OPTS)
    assert report["population"]["conclusive"] is False
    assert report["judgement"][GRAPH] == "inconclusive"
    assert report["no_hub"]["judgement"][GRAPH] == "inconclusive"
    small = run_replay(repo.root, min_population=5, **OPTS)
    assert small["population"]["conclusive"] is True
    assert small["judgement"][GRAPH] in ("pass", "fail")
    assert "INCONCLUSIVE" in format_replay(report)
    assert "INCONCLUSIVE" not in format_replay(small)


# -- incremental resolution, cache, determinism ------------------------------------

def test_incremental_resolution_equals_from_scratch(history):
    repo, _c = history
    fast_stats, slow_stats = {}, {}
    fast = run_replay(repo.root, per_commit=True, stats=fast_stats, **OPTS)
    slow = run_replay(repo.root, per_commit=True, incremental=False, stats=slow_stats, **OPTS)
    assert json.dumps(fast, sort_keys=True) == json.dumps(slow, sort_keys=True)
    assert fast_stats["docs_resolved"] < slow_stats["docs_resolved"]


def test_cache_hit_reads_no_blobs_second_time(history, tmp_path):
    repo, _c = history
    cache = tmp_path / "cache" / "replay.db"
    opts = dict(OPTS, use_disk_cache=True, cache_path=cache)
    first_stats, second_stats = {}, {}
    first = run_replay(repo.root, stats=first_stats, **opts)
    second = run_replay(repo.root, stats=second_stats, **opts)
    assert cache.exists()
    assert first_stats["blob_reads"] > 0
    assert second_stats["blob_reads"] == 0
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_cache_miss_on_fingerprint_change(history, tmp_path):
    repo, _c = history
    cache = tmp_path / "replay.db"
    opts = dict(OPTS, use_disk_cache=True, cache_path=cache)
    run_replay(repo.root, **opts)
    stats = {}
    other = graph_settings({"graph": {"identifiers": {"min_length": 4}}})
    run_replay(repo.root, settings=other, stats=stats, **opts)
    assert stats["blob_reads"] > 0


def test_default_cache_lives_under_carta_home_and_not_in_the_repo(history, tmp_path, monkeypatch):
    repo, _c = history
    home = tmp_path / "carta-home"
    monkeypatch.setenv("CARTA_HOME", str(home))
    fake_user_home = tmp_path / "user-home"
    fake_user_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_user_home))
    run_replay(repo.root, **dict(OPTS, use_disk_cache=True))
    assert len(list((home / "cache" / "graph-replay").glob("*.db"))) == 1
    assert list(fake_user_home.iterdir()) == []
    assert not (repo.root / ".carta").exists()


def test_report_is_deterministic(history, tmp_path):
    repo, _c = history
    plain = run_replay(repo.root, per_commit=True, **OPTS)
    again = run_replay(repo.root, per_commit=True, **OPTS)
    cached = run_replay(repo.root, per_commit=True,
                        **dict(OPTS, use_disk_cache=True, cache_path=tmp_path / "c.db"))
    text = json.dumps(plain, sort_keys=True)
    assert text == json.dumps(again, sort_keys=True) == json.dumps(cached, sort_keys=True)
    assert format_replay(plain) == format_replay(again)


def test_report_has_no_timestamps_or_absolute_paths(history, tmp_path):
    repo, _c = history
    report = run_replay(repo.root, per_commit=True, **OPTS)
    text = json.dumps(report) + format_replay(report)
    assert str(tmp_path) not in text and str(repo.root) not in text
    assert "seconds" not in text and "2020-" not in text


# -- arms and providers ------------------------------------------------------------

def test_arms_unknown_kind_is_a_clear_error(history):
    repo, _c = history
    with pytest.raises(ReplayError, match="cochange"):
        run_replay(repo.root, arms={"x": ["cochange"]}, **OPTS)
    with pytest.raises(ReplayError):
        run_replay(repo.root, arms={"empty": []}, **OPTS)
    with pytest.raises(ReplayError):
        run_replay(repo.root, arms={"popularity": ["mention"], "bad name!": ["mention"]}, **OPTS)


class _CoChange:
    """A toy history-mined provider: a doc is linked to every file it was committed with."""

    kind = "cochange"

    def __init__(self, context) -> None:
        self.pairs = {}
        self.calls = []
        self.context = context

    def edges_at(self, snapshot):
        self.calls.append(("edges_at", snapshot.parent, len(snapshot.prior_changes)))
        docs = set(snapshot.docs)
        return sorted(
            Edge("cochange", src, dst, "doc", src_type="file", weight=float(n),
                 extractor="cochange")
            for (src, dst), n in self.pairs.items() if dst in docs)

    def observe(self, commit, changes):
        self.calls.append(("observe", commit, len(changes)))
        docs = [p for _s, p in changes if p.endswith(".md")]
        if len(changes) > 4:                        # a bulk commit couples everything
            return
        for _status, path in changes:
            if not path.endswith(".md"):
                for doc in docs:
                    self.pairs[(path, doc)] = self.pairs.get((path, doc), 0) + 1


def test_custom_provider_can_be_registered_and_measured(history, monkeypatch):
    repo, c = history
    made = []

    def factory(context):
        made.append(_CoChange(context))
        return made[-1]

    monkeypatch.setitem(PROVIDERS, "cochange", factory)
    arms = {"mention": ["mention"], "cochange": ["cochange"], "both": ["mention", "cochange"]}
    report = run_replay(repo.root, arms=arms, per_commit=True, **OPTS)
    names = {"graph[mention]", "graph[cochange]", "graph[both]"}
    assert names <= set(report["methods"]) and names <= set(report["no_hub"]["methods"])
    assert report["settings"]["arms"] == {
        "both": ["cochange", "mention"], "cochange": ["cochange"], "mention": ["mention"]}
    # every arm is paired with every baseline and with every other arm
    assert set(report["paired"]["graph[both]"]) == {
        *BASELINES, "graph[mention]", "graph[cochange]"}
    by = rows(report)
    # tools/gen.py had been committed with the changelog before 'main_between':
    # co-change finds it, mentions cannot, and the union finds it too.
    row = by[c["main_between"]]["ranks"]
    assert row["graph[cochange]"] == 1 and row["graph[mention]"] is None
    assert row["graph[both]"] == 1
    # One provider instance per run, observed once per mainline commit, in order,
    # and never asked about a commit before being told about the ones before it.
    assert len(made) == 1
    provider = made[0]
    observed = [commit for what, commit, _n in provider.calls if what == "observe"]
    assert observed == [e.sha for e in first_parent_log(repo.root, "HEAD")]
    seen = set()
    for what, commit, n_prior in provider.calls:
        if what == "observe":
            seen.add(commit)
        else:
            assert commit in seen and n_prior == len(seen)
    # the trap commit itself is not in what the provider knew when it was scored
    assert by[c["trap"]]["ranks"]["graph[cochange]"] is None


def test_register_provider(monkeypatch):
    monkeypatch.setattr(replay_mod, "PROVIDERS", dict(PROVIDERS))
    register_provider("toy", _CoChange)
    assert replay_mod.PROVIDERS["toy"] is _CoChange
    assert "mention" in PROVIDERS


# -- window selection and degenerate histories ------------------------------------

def test_range_and_last_selection(history):
    repo, c = history
    last = run_replay(repo.root, last=3, per_commit=True, **OPTS)
    assert last["population"]["commits_scanned"] == 3
    assert last["repo"]["range"] == "last 3"
    assert [r["commit"] for r in last["per_commit"]] == [c["trap"], c["after_trap"]]
    # warm-up: popularity still knows the history before the window
    assert rows(last)[c["trap"]]["ranks"]["popularity"] is not None
    full = rows(run_replay(repo.root, per_commit=True, **OPTS))
    assert rows(last)[c["trap"]]["ranks"] == full[c["trap"]]["ranks"]

    spec = f"{c['parser']}..{c['changelog_only']}"
    ranged = run_replay(repo.root, range_spec=spec, per_commit=True, **OPTS)
    assert ranged["population"]["commits_scanned"] == 3
    assert ranged["repo"] == {"head": c["changelog_only"], "range": spec}
    assert [r["commit"] for r in ranged["per_commit"]] == [
        c["store"], c["parser_changelog"], c["changelog_only"]]
    assert rows(ranged)[c["store"]]["ranks"] == full[c["store"]]["ranks"]

    with pytest.raises(ReplayError):
        run_replay(repo.root, range_spec=spec, last=3, **OPTS)
    with pytest.raises(ReplayError):
        run_replay(repo.root, range_spec="no-such-ref..HEAD", **OPTS)
    empty = run_replay(repo.root, range_spec="HEAD..HEAD", **OPTS)
    assert empty["population"]["commits_scanned"] == 0 and empty["population"]["eligible"] == 0


def test_shallow_or_tiny_history_degrades_to_empty_population_not_error(git_repo, tmp_path):
    git_repo.write("README.md", "# x\n")
    git_repo.write("src/a.py", "x\n")
    git_repo.commit("only", 0)
    report = run_replay(git_repo.root, **OPTS)
    assert report["population"]["commits_scanned"] == 1
    assert report["population"]["eligible"] == 0
    assert report["population"]["skipped"]["root_commit"] == 1
    assert report["methods"][GRAPH]["n"] == 0 and report["methods"][GRAPH]["mrr"] == 0.0
    assert "Population: 1 commit scanned, 0 eligible" in format_replay(report)
    with pytest.raises(ReplayError):
        run_replay(tmp_path / "not-a-repo", **OPTS)


def test_shallow_clone_is_replayed_from_its_boundary(history, tmp_path):
    repo, c = history
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "4", "file://" + str(repo.root), str(clone)],
                   check=True, capture_output=True)
    report = run_replay(clone, per_commit=True, **OPTS)
    assert report["population"]["commits_scanned"] == 4
    assert report["population"]["skipped"]["root_commit"] == 1
    full = rows(run_replay(repo.root, per_commit=True, **OPTS))
    got = rows(report)
    assert set(got) == {c["trap"], c["after_trap"]}
    assert got[c["after_trap"]]["ranks"][GRAPH] == full[c["after_trap"]]["ranks"][GRAPH] == 1


def test_works_without_carta_config_and_from_a_subdirectory(history):
    repo, _c = history
    assert not (repo.root / ".carta").exists()
    top = run_replay(repo.root, **OPTS)
    sub = run_replay(repo.root / "docs", **OPTS)
    assert top == sub


def test_doc_globs_decide_what_a_doc_is(history):
    repo, _c = history
    only_docs_dir = graph_settings({"graph": {"doc_globs": ["docs/**/*.md"]}})
    report = run_replay(repo.root, settings=only_docs_dir, **OPTS)
    # CHANGELOG.md and README.md are now ordinary files: the two changelog-only
    # commits have no gold and become source-only.
    assert report["population"]["eligible"] == 7
    assert report["population"]["skipped"]["no_docs_edited"] == 3
    assert report["population"]["docs_at_end"] == 4


# -- output ----------------------------------------------------------------------

def test_json_shape_is_the_spec(history):
    repo, _c = history
    report = run_replay(repo.root, **OPTS)
    assert set(report) == {"schema", "repo", "settings", "population", "methods", "paired",
                           "judgement", "no_hub", "alarm"}
    assert report["schema"] == 1
    assert set(report["population"]) == {
        "commits_scanned", "eligible", "skipped", "gold_docs_mean", "gold_docs_median",
        "docs_at_end", "new_docs_not_gold", "excluded_doc_paths", "conclusive"}
    method = report["methods"][GRAPH]
    assert set(method) == {"n", "recall_at", "precision_at", "any_hit_at", "mrr",
                           "mean_returned", "ci95"}
    assert set(method["recall_at"]) == {"1", "3", "5", "10"}
    assert set(method["ci95"]) == {"recall_at_5", "any_hit_at_5", "mrr"}
    assert set(report["no_hub"]) == {"hub_docs", "population", "methods", "paired", "judgement"}
    assert set(report["settings"]) == {
        "arms", "ks", "hub_share", "max_files", "popularity_window", "min_population",
        "resamples", "doc_globs", "fingerprint", "weights"}
    assert "per_commit" not in report
    with_rows = run_replay(repo.root, per_commit=True, **OPTS)
    assert set(with_rows["per_commit"][0]) == {
        "commit", "n_query", "n_gold", "gold", "ranks", "no_hub_ranks"}
    custom = run_replay(repo.root, ks=(2, 4), **OPTS)
    assert set(custom["methods"][GRAPH]["recall_at"]) == {"2", "4"}
    assert set(custom["methods"][GRAPH]["ci95"]) == {"recall_at_4", "any_hit_at_4", "mrr"}

    def no_long_floats(node):
        if isinstance(node, float):
            assert round(node, 6) == node
        elif isinstance(node, dict):
            for value in node.values():
                no_long_floats(value)
        elif isinstance(node, list):
            for value in node:
                no_long_floats(value)
    no_long_floats(with_rows)


def test_format_replay_states_population_first(history):
    repo, c = history
    text = format_replay(run_replay(repo.root, **OPTS))
    lines = text.splitlines()
    assert lines[0] == f"Replay  repo={c['after_trap'][:12]}  range=all"
    assert lines[1] == "Population: 15 commits scanned, 9 eligible (60.0%)"
    assert "skipped:" in lines[2] and "no_docs_edited 1" in lines[2] and "bulk 1" in lines[2]
    table_at = next(i for i, line in enumerate(lines) if "R@1" in line)
    assert table_at > 2
    for name in (GRAPH, *BASELINES):
        assert any(line.startswith(name) for line in lines[table_at:]), name
    assert f"{GRAPH} vs popularity" in text and "dMRR" in text
    assert "Without hub docs (>40% of eligible commits: CHANGELOG.md 44%): 7 eligible" in text
    assert "Source-only commits (1, unlabelled)" in text


# -- entry points ------------------------------------------------------------------

def test_module_main_runs(history, tmp_path, capsys):
    repo, c = history
    out_file = tmp_path / "out" / "report.json"
    code = main(["--repo", str(repo.root), "--max-files", "5", "--hub-share", "0.4",
                 "--no-cache", "--resamples", "100", "--json", str(out_file)])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("Replay  repo=")
    assert "commits/s" in captured.err
    written = json.loads(out_file.read_text())
    assert written["population"]["eligible"] == 9

    code = main(["--repo", str(repo.root), "--max-files", "5", "--no-cache", "--resamples", "50",
                 "--last", "5", "--k", "1,2", "--per-commit", "--arm", "m=mention", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    report = json.loads(captured.out)
    assert report["population"]["commits_scanned"] == 5
    assert set(report["methods"]) == {"graph[m]", *BASELINES}
    assert set(report["methods"]["graph[m]"]["recall_at"]) == {"1", "2"}
    assert "per_commit" in report


def test_main_errors_are_messages_not_tracebacks(history, tmp_path, capsys):
    repo, _c = history
    assert main(["--repo", str(tmp_path / "nowhere"), "--no-cache"]) == 1
    assert "Error:" in capsys.readouterr().err
    assert main(["--repo", str(repo.root), "--no-cache", "--arm", "x=cochange"]) == 1
    assert "cochange" in capsys.readouterr().err
    assert main(["--repo", str(repo.root), "--no-cache", "--arm", "nonsense"]) == 1
    assert main(["--repo", str(repo.root), "--no-cache", "--k", "0,three"]) == 1
    assert main(["--repo", str(repo.root), "--no-cache", "--last", "2", "--range", "a..b"]) == 1
    capsys.readouterr()


def test_main_reads_graph_settings_from_the_repo_config_when_present(history, capsys):
    repo, _c = history
    repo.write(".carta/config.yaml", "graph:\n  doc_globs: ['docs/**/*.md']\n")
    assert main(["--repo", str(repo.root), "--no-cache", "--max-files", "5",
                 "--resamples", "50", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["population"]["eligible"] == 7
    assert main(["--repo", str(repo.root), "--no-cache", "--max-files", "5", "--resamples", "50",
                 "--doc-glob", "**/*.md", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["population"]["eligible"] == 9
    repo.write(".carta/config.yaml", "graph: [not, a, mapping]\n")
    assert main(["--repo", str(repo.root), "--no-cache"]) == 1
    assert "graph" in capsys.readouterr().err


def test_python_dash_m_entry_point(history):
    import sys
    repo, _c = history
    proc = subprocess.run(
        [sys.executable, "-m", "carta.graph.eval.replay", "--repo", str(repo.root),
         "--no-cache", "--max-files", "5", "--resamples", "50"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("Replay  repo=")


# -- budget ------------------------------------------------------------------------

def _fast_import(repo, n_commits: int, n_docs: int) -> None:
    """Build a long linear history in one git process."""
    out = []
    mark = 0

    def commit(message: str, files: dict) -> None:
        nonlocal mark
        mark += 1
        out.append(f"commit refs/heads/main\nmark :{mark}\n")
        stamp = 1577836800 + mark * 3600
        out.append(f"committer Test Author <author@example.invalid> {stamp} +0000\n")
        out.append(f"data {len(message.encode())}\n{message}\n")
        if mark > 1:
            out.append(f"from :{mark - 1}\n")
        for path, text in sorted(files.items()):
            if text is None:
                out.append(f"D {path}\n")
            else:
                data = text.encode()
                out.append(f"M 100644 inline {path}\ndata {len(data)}\n{text}\n")

    def doc(i: int, version: int) -> str:
        body = [f"# Doc {i} v{version}", ""]
        for j in range(40):
            mod = (i * 7 + j) % 60
            body.append(f"Line {j}: see `src/pkg{mod % 6}/mod{mod}.py` and `mod{(mod + 1) % 60}.py`,"
                        f" the `src/pkg{j % 6}/` package and `helper_{j}()`.")
        return "\n".join(body) + "\n"

    files = {f"src/pkg{m % 6}/mod{m}.py": f"X = {m}\n" for m in range(60)}
    files.update({f"docs/area{i % 5}/doc{i}.md": doc(i, 0) for i in range(n_docs)})
    commit("root", files)
    for n in range(1, n_commits):
        mod = (n * 13) % 60
        change = {f"src/pkg{mod % 6}/mod{mod}.py": f"X = {mod}\nN = {n}\n"}
        if n % 2 == 0:
            i = (n * 3) % n_docs
            change[f"docs/area{i % 5}/doc{i}.md"] = doc(i, n)
        if n % 10 == 0:                              # path-set churn: add, later delete
            change[f"src/pkg{n % 6}/extra{n}.py"] = "E = 1\n"
        if n % 10 == 5 and n > 10:
            change[f"src/pkg{(n - 5) % 6}/extra{n - 5}.py"] = None
        commit(f"change {n}", change)
    stream = "".join(out).encode()
    proc = subprocess.run(["git", "fast-import", "--quiet"], cwd=str(repo.root), input=stream,
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_replay_budget_and_incremental_equivalence_on_a_long_history(git_repo):
    _fast_import(git_repo, n_commits=200, n_docs=100)
    stats = {}
    started = time.perf_counter()
    report = run_replay(git_repo.root, use_disk_cache=False, resamples=100, stats=stats)
    elapsed = time.perf_counter() - started
    assert report["population"]["commits_scanned"] == 200
    assert report["population"]["eligible"] == 99
    assert report["methods"][GRAPH]["any_hit_at"]["10"] > 0
    assert elapsed < 30, f"replay took {elapsed:.1f}s"
    # selective re-resolution: far fewer than every doc at every commit
    assert stats["docs_resolved"] < 200 * 100 / 20
    # Re-resolving every doc at every commit must give the same report (a shorter
    # window keeps this half of the test quick).
    opts = dict(use_disk_cache=False, resamples=100, last=50, per_commit=True)
    lean = run_replay(git_repo.root, **opts)
    slow = run_replay(git_repo.root, incremental=False, **opts)
    assert json.dumps(slow, sort_keys=True) == json.dumps(lean, sort_keys=True)
