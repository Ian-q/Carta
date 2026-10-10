"""Mention, co-change and symbol edges together: one store, one impact query, one replay arm.

Each kind has its own tests. These cover what only shows when all three are
on: the build runs three passes into one store, ``carta impact`` fuses three
rankings, and a replay arm may read all three providers.
"""
from __future__ import annotations

import json

import pytest

from carta.graph.build import build_graph, graph_status
from carta.graph.eval.replay import format_replay, main, run_replay
from carta.graph.eval.symbol_replay import make_source
from carta.graph.query import format_impact, run_impact
from carta.graph.settings import DEFAULT_SETTINGS
from carta.graph.store import EdgeStore, store_path
from carta.graph.tests.conftest import requires_git

pytestmark = requires_git

KINDS = ["cochange", "mention", "symbol"]
CFG = {"graph": {"symbols": {"provider": "file", "file": "index/symbols.jsonl"}}}
INDEX = json.dumps({"name": "parse_frame", "kind": "function", "file": "src/a.py",
                    "line": 1}) + "\n"


@pytest.fixture
def project(git_repo):
    """Three docs, each tied to ``src/a.py`` by a different kind of edge.

    ``docs/named.md`` names the file, ``docs/api.md`` names a function it
    defines, ``docs/moves.md`` names nothing and changes with it.
    """
    repo = git_repo
    repo.write(".gitignore", ".carta/\n")
    repo.write("index/symbols.jsonl", INDEX)
    repo.write("src/a.py", "def parse_frame(raw):\n    return raw\n")
    repo.write("docs/named.md", "The parser is `src/a.py`.\n")
    repo.write("docs/api.md", "Call `parse_frame()` first.\n")
    repo.write("docs/moves.md", "Release notes.\n")
    c = {"root": repo.commit("root", 0)}
    for day in range(1, 4):
        repo.write("src/a.py", f"def parse_frame(raw):\n    return raw  # {day}\n")
        repo.write("docs/moves.md", f"Release notes {day}.\n")
        repo.commit(f"day {day}", day)
    repo.write("src/a.py", "def parse_frame(raw):\n    return bytes(raw)\n")
    repo.write("docs/api.md", "Call `parse_frame()` first. It returns bytes.\n")
    c["api"] = repo.commit("the function and the doc that names it", 4)
    repo.write("src/a.py", "def parse_frame(raw):\n    return bytes(raw)  # tidy\n")
    repo.write("docs/named.md", "The parser is `src/a.py`. Tidied.\n")
    repo.write("docs/moves.md", "Release notes 5.\n")
    c["both"] = repo.commit("the doc that names the file, and the notes", 5)
    repo.commits = c
    return repo


def digests(root) -> dict:
    with EdgeStore.open_readonly(store_path(root)) as store:
        return {kind: store.edges_digest((kind,)) for kind in KINDS}


def test_one_build_writes_three_kinds_and_a_second_changes_nothing(project):
    first = build_graph(project.root, CFG)
    assert first["cochange"]["status"] == "ok" and first["symbols"]["state"] == "ok"
    assert set(first["by_kind"]) >= set(KINDS)
    assert all(first["by_kind"][kind] > 0 for kind in KINDS)
    assert first["edges"] == sum(first["by_kind"].values())
    before = digests(project.root)
    status = graph_status(project.root, CFG)
    assert not status["stale"] and status["stale_reasons"] == []
    assert status["cochange"]["enabled"] and status["symbols"]["state"] == "ok"

    again = build_graph(project.root, CFG)
    assert again["extracted"] == 0 and again["cochange"]["new_commits"] == 0
    assert again["symbols"]["unchanged"] is True
    assert (again["edges"], again["by_kind"]) == (first["edges"], first["by_kind"])
    assert digests(project.root) == before


def test_each_pass_leaves_the_other_two_kinds_alone(project):
    build_graph(project.root, CFG)
    before = digests(project.root)
    # The history pass skipped: symbol and mention edges are still brought up to date.
    skipped = build_graph(project.root, CFG, cochange=False, force=True)
    assert skipped["cochange"]["status"] == "skipped" and skipped["symbols"]["state"] == "ok"
    assert digests(project.root) == before
    # The symbol provider turned off: only the symbol edges go.
    off = build_graph(project.root, {})
    assert off["symbols"]["state"] == "off" and "symbol" not in off["by_kind"]
    after = digests(project.root)
    assert (after["mention"], after["cochange"]) == (before["mention"], before["cochange"])
    assert not graph_status(project.root, {})["stale"]
    # History turned off: only the co-change edges go.
    no_history = {"graph": dict(CFG["graph"], cochange={"enabled": False})}
    report = build_graph(project.root, no_history)
    assert "cochange" not in report["by_kind"] and report["by_kind"]["symbol"] > 0
    after = digests(project.root)
    assert (after["mention"], after["symbol"]) == (before["mention"], before["symbol"])
    assert not graph_status(project.root, no_history)["stale"]


def test_impact_reads_all_three_kinds_by_default_once_a_provider_is_named(project):
    out = run_impact(project.root, CFG, paths=["src/a.py"])
    assert out["query"]["kinds"] == KINDS
    found = {r["doc"]: sorted(m["kind"] for m in r["matches"]) for r in out["results"]}
    # Every doc changed with the file at least once; two are also tied to it by what they say.
    assert found == {"docs/named.md": ["cochange", "mention"],
                     "docs/api.md": ["cochange", "symbol"], "docs/moves.md": ["cochange"]}
    assert out["symbols"]["state"] == "ok" and out["symbols"]["stale"] is False
    assert out["unmatched_paths"] == []
    text = format_impact(out)
    assert "3 docs are tied to 1 path" in text
    assert "[history] changed together in" in text
    assert "[named] exact, named by 1 doc" in text
    assert "[named] symbol: the doc names an identifier defined here" in text
    # Any one kind can still be read alone, and no provider means no symbol kind.
    for kind, doc in (("mention", "docs/named.md"), ("symbol", "docs/api.md"),
                      ("cochange", "docs/moves.md")):
        alone = run_impact(project.root, CFG, paths=["src/a.py"], kinds=[kind])
        assert alone["results"][0]["doc"] == doc
        assert {m["kind"] for r in alone["results"] for m in r["matches"]} == {kind}
    assert run_impact(project.root, {}, paths=["src/a.py"])["query"]["kinds"] == [
        "cochange", "mention"]


@pytest.mark.parametrize("mode", ["norm", "rrf", "sum", "max"])
def test_a_replay_arm_can_read_all_three_kinds(project, mode):
    source = make_source(project.root, "tracked:index/symbols.jsonl", accept_leak=False,
                         settings=DEFAULT_SETTINGS)
    arms = {"all": ["mention", "cochange", "symbol", "@" + mode], "mention": ["mention"],
            "cochange": ["cochange"], "symbol": ["symbol"]}
    report = run_replay(project.root, symbols=source, arms=arms, use_disk_cache=False,
                        resamples=50, per_commit=True, min_population=1)
    assert report["settings"]["arms"]["all"] == KINDS
    assert report["settings"]["combine"]["all"] == mode
    assert report["symbols"]["leak"] is False and report["settings"]["cochange"]
    rows = {row["commit"]: row["ranks"] for row in report["per_commit"]}
    c = project.commits
    # Only the symbol edge knows docs/api.md; the union finds it too.
    api = rows[c["api"]]
    assert (api["graph[mention]"], api["graph[cochange]"], api["graph[symbol]"]) == (
        None, None, 1)
    assert api["graph[all]"] is not None
    # Mentions and history each find one of the two gold docs, the symbol edge neither;
    # the union still has a gold doc in its first two.
    both = rows[c["both"]]
    assert both["graph[mention]"] == 1 and both["graph[cochange]"] == 1
    assert both["graph[symbol]"] is None and both["graph[all]"] in (1, 2)
    assert "graph[all]" in format_replay(report)


def test_the_command_line_runs_a_three_kind_arm(project, tmp_path, capsys):
    out = tmp_path / "report.json"
    code = main(["--repo", str(project.root), "--no-cache", "--no-config", "--resamples", "50",
                 "--arm", "all=mention+cochange+symbol@rrf", "--arm", "pair=mention+cochange",
                 "--symbols", "tracked:index/symbols.jsonl", "--json", str(out)])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["settings"]["arms"] == {"all": KINDS, "pair": ["cochange", "mention"]}
    assert report["settings"]["combine"]["all"] == "rrf"
    assert {"graph[all]", "graph[pair]"} <= set(report["methods"])
    assert "Symbols: tracked:index/symbols.jsonl" in capsys.readouterr().out
