"""Impact ranking (pure) and the impact query over a real tree."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from carta.graph.model import Edge
from carta.graph.query import (
    DEFAULT_KIND_WEIGHTS, ImpactError, ImpactIndex, format_impact, normalise_query_paths,
    rank_impact, run_impact,
)
from carta.graph.store import store_path
from carta.graph.tests.conftest import requires_git

LN2 = math.log(2)


def m(doc: str, dst: str, dst_type: str = "file", line: int = 1, weight: float = 1.0,
      kind: str = "mention", evidence: str = "") -> Edge:
    return Edge(kind=kind, src=doc, dst=dst, dst_type=dst_type, line=line,
                evidence=evidence or f"{doc}:{line} names {dst}", weight=weight)


def cochange(src: str, dst: str, weight: float, dst_type: str = "doc") -> Edge:
    return Edge(kind="cochange", src=src, dst=dst, dst_type=dst_type, weight=weight,
                evidence="changed together in 4 commits", src_type="file",
                extractor="cochange", extractor_version=1)


def rank(edges, paths, n_docs, **kw):
    return rank_impact(ImpactIndex(edges, n_docs), paths, **kw)


def scores(results):
    return {r["doc"]: r["score"] for r in results}


# -- the formula ----------------------------------------------------------

def test_exact_match_scores_formula():
    out = rank([m("docs/a.md", "src/a.py")], ["src/a.py"], 10)
    assert [r["doc"] for r in out] == ["docs/a.md"]
    assert out[0]["score"] == pytest.approx(math.log(11), abs=1e-9)
    assert out[0]["rank"] == 1


def test_rarer_target_ranks_higher():
    edges = [m("docs/a.md", "src/rare.py"), m("docs/x.md", "src/rare.py"),
             m("docs/b.md", "src/common.py")]
    edges += [m(f"docs/c{i}.md", "src/common.py") for i in range(7)]
    index = ImpactIndex(edges, 10)
    assert index.df("mention", "src/rare.py") == 2
    assert index.df("mention", "src/common.py") == 8
    a = rank_impact(index, ["src/rare.py"])[0]
    b = rank_impact(index, ["src/common.py"])[0]
    assert a["score"] == pytest.approx(math.log(1 + 10 / 2))
    assert b["score"] == pytest.approx(math.log(1 + 10 / 8))
    both = [r["doc"] for r in rank_impact(index, ["src/rare.py", "src/common.py"], top=None)]
    assert both.index("docs/a.md") < both.index("docs/b.md")


def test_dir_match_discounted_by_depth():
    edges = [m("docs/a.md", "src/", "dir")]
    assert rank(edges, ["src/a.py"], 1)[0]["score"] == pytest.approx(LN2 / 2)
    assert rank(edges, ["src/p/a.py"], 1)[0]["score"] == pytest.approx(LN2 / 3)
    assert rank(edges, ["other/a.py"], 1) == []
    assert rank(edges, ["srcx/a.py"], 1) == []          # a prefix, not a parent


def test_exact_beats_dir_for_same_doc_and_is_not_double_counted():
    edges = [m("docs/a.md", "src/a.py"), m("docs/a.md", "src/", "dir", line=2)]
    out = rank(edges, ["src/a.py"], 1)
    assert out[0]["score"] == pytest.approx(LN2)
    assert [(x["target"], x["match"]) for x in out[0]["matches"]] == [("src/a.py", "exact")]


def test_glob_match_half_weight():
    edges = [m("docs/a.md", "src/**/*.py", "glob")]
    out = rank(edges, ["src/p/a.py"], 1)
    assert out[0]["score"] == pytest.approx(LN2 / 2)
    assert out[0]["matches"][0]["match"] == "glob"
    assert rank(edges, ["src/p/a.txt"], 1) == []


def test_query_directory_matches_targets_under_it():
    edges = [m("docs/a.md", "src/parser/frame.py"), m("docs/b.md", "lib/x.py"),
             m("docs/c.md", "src/parser/", "dir")]
    out = rank(edges, ["src/parser/"], 3)
    assert [r["doc"] for r in out] == ["docs/a.md", "docs/c.md"]
    assert out[0]["matches"][0]["match"] == "under"
    assert out[1]["matches"][0]["match"] == "exact"
    assert out[0]["score"] == pytest.approx(math.log(4))


def test_query_directory_is_also_matched_by_a_parent_directory_mention():
    out = rank([m("docs/a.md", "src/", "dir")], ["src/parser/"], 1)
    assert out[0]["matches"][0]["match"] == "dir"
    assert out[0]["score"] == pytest.approx(LN2 / 2)


def test_missing_edge_matches_exact_query():
    out = rank([m("docs/a.md", "src/planned.py", "missing")], ["src/planned.py"], 1)
    assert out[0]["matches"][0]["target_type"] == "missing"
    assert out[0]["score"] == pytest.approx(LN2)


def test_repeated_mentions_grow_logarithmically():
    edges = [m("docs/a.md", "src/a.py", line=n) for n in (3, 9, 27)]
    assert rank(edges, ["src/a.py"], 1)[0]["score"] == pytest.approx(LN2 * (1 + math.log(3)))


def test_fence_only_mention_is_half():
    assert rank([m("docs/a.md", "src/a.py", weight=0.5)], ["src/a.py"], 1)[0]["score"] == \
        pytest.approx(LN2 / 2)
    mixed = [m("docs/a.md", "src/a.py", weight=0.5), m("docs/a.md", "src/a.py", line=2)]
    assert rank(mixed, ["src/a.py"], 1)[0]["score"] == pytest.approx(LN2 * (1 + LN2))


def test_score_sums_across_query_paths():
    edges = [m("docs/a.md", "src/a.py"), m("docs/a.md", "src/b.py", line=2),
             m("docs/b.md", "src/a.py")]
    out = rank(edges, ["src/a.py", "src/b.py"], 2)
    assert scores(out) == {
        "docs/a.md": pytest.approx(math.log(2) + math.log(3)),
        "docs/b.md": pytest.approx(math.log(2)),
    }
    assert [r["doc"] for r in out] == ["docs/a.md", "docs/b.md"]


def test_ident_and_pattern_edges_never_match_paths():
    edges = [m("docs/a.md", "src/a.py", "token", kind="ident"),
             m("docs/a.md", "msg:src/a.py", "pattern", kind="pattern"),
             m("docs/a.md", "src/a.py", "token")]
    assert rank(edges, ["src/a.py", "msg:src/a.py"], 1,
                kinds=("mention", "ident", "pattern")) == []


# -- per-target aggregation of directory and glob matches -----------------

def test_dir_match_is_aggregated_per_target_not_per_path():
    edges = [m("docs/a.md", "src/", "dir")]
    out = rank(edges, ["src/a.py", "src/b.py", "src/c.py"], 1)
    assert out[0]["score"] == pytest.approx((LN2 / 2) * (1 + math.log(3)))
    assert sum(x["contribution"] for x in out[0]["matches"]) == pytest.approx(out[0]["score"])
    assert [x["path"] for x in out[0]["matches"]] == ["src/a.py", "src/b.py", "src/c.py"]


def test_each_path_is_assigned_to_its_best_directory_target():
    edges = [m("docs/a.md", "src/", "dir"), m("docs/a.md", "src/x/", "dir", line=2)]
    out = rank(edges, ["src/x/a.py", "src/x/b.py", "src/top.py"], 1)
    by_path = {x["path"]: x["target"] for x in out[0]["matches"]}
    assert by_path == {"src/x/a.py": "src/x/", "src/x/b.py": "src/x/", "src/top.py": "src/"}
    assert out[0]["score"] == pytest.approx((LN2 / 2) * (1 + LN2) + LN2 / 2)


def test_exact_mention_of_one_file_beats_a_directory_mention_that_covers_ten():
    files = [f"src/x/f{i}.py" for i in range(10)]
    edges = [m("docs/exact.md", files[0])]
    edges += [m(f"docs/d{i}.md", "src/x/", "dir") for i in range(5)]
    out = rank(edges, files, 20, top=None)
    assert out[0]["doc"] == "docs/exact.md"
    assert out[0]["score"] == pytest.approx(math.log(21))
    assert out[1]["score"] == pytest.approx(0.5 * math.log(5) * (1 + math.log(10)))


# -- other kinds -----------------------------------------------------------

def test_kinds_filter_and_kind_weights():
    edges = [m("docs/a.md", "src/a.py"), cochange("src/a.py", "docs/b.md", 0.4),
             cochange("docs/b.md", "src/a.py", 0.4, dst_type="file")]
    assert [r["doc"] for r in rank(edges, ["src/a.py"], 5)] == ["docs/a.md"]
    out = rank(edges, ["src/a.py"], 5, kinds=("cochange",), weights={"cochange": 0.5})
    assert scores(out) == {"docs/b.md": pytest.approx(0.2)}
    assert out[0]["matches"][0]["match"] == "cochange"
    assert out[0]["matches"][0]["evidence"] == [
        {"line": 0, "text": "changed together in 4 commits"}]
    both = rank(edges, ["src/a.py"], 5, kinds=("mention", "cochange"))
    assert [r["doc"] for r in both] == ["docs/a.md", "docs/b.md"]
    assert both[1]["score"] == pytest.approx(DEFAULT_KIND_WEIGHTS["cochange"] * 0.4)


def test_file_sourced_edge_is_read_in_one_direction_only():
    edges = [cochange("docs/b.md", "src/a.py", 0.4, dst_type="file"),
             cochange("src/a.py", "src/b.py", 0.9, dst_type="file")]
    assert rank(edges, ["src/a.py", "docs/b.md", "src/b.py"], 5, kinds=("cochange",)) == []


def test_symbol_edges_rank_like_mentions_with_their_own_weight():
    edge = Edge(kind="symbol", src="docs/a.md", dst="src/a.py", dst_type="file", line=4,
                key="parse_frame", evidence="calls `parse_frame()`", extractor="symbol")
    out = rank([edge], ["src/a.py"], 1, kinds=("symbol",))
    assert out[0]["score"] == pytest.approx(DEFAULT_KIND_WEIGHTS["symbol"] * LN2)


# -- ordering, limits, payload --------------------------------------------

def test_tie_break_is_path_ascending():
    edges = [m("docs/b.md", "src/a.py"), m("docs/a.md", "src/a.py"), m("docs/c.md", "src/a.py")]
    assert [r["doc"] for r in rank(edges, ["src/a.py"], 3)] == [
        "docs/a.md", "docs/b.md", "docs/c.md"]


def test_top_and_min_score():
    edges = [m("docs/a.md", "src/a.py"), m("docs/a.md", "src/a.py", line=2),
             m("docs/b.md", "src/a.py"), m("docs/c.md", "src/a.py", weight=0.5)]
    full = rank(edges, ["src/a.py"], 3, top=None)
    assert [r["doc"] for r in full] == ["docs/a.md", "docs/b.md", "docs/c.md"]
    assert [r["rank"] for r in full] == [1, 2, 3]
    assert [r["doc"] for r in rank(edges, ["src/a.py"], 3, top=2)] == ["docs/a.md", "docs/b.md"]
    cut = full[1]["score"]
    assert [r["doc"] for r in rank(edges, ["src/a.py"], 3, min_score=cut)] == [
        "docs/a.md", "docs/b.md"]


def test_exclude_docs():
    edges = [m("docs/a.md", "src/a.py"), m("docs/b.md", "src/a.py")]
    out = rank(edges, ["src/a.py"], 2, exclude_docs={"docs/a.md"})
    assert [(r["rank"], r["doc"]) for r in out] == [(1, "docs/b.md")]


def test_evidence_lines_sorted_and_complete():
    edges = [m("docs/a.md", "src/a.py", line=n, evidence=f"line {n}") for n in (30, 4, 12, 7)]
    match = rank(edges, ["src/a.py"], 1)[0]["matches"][0]
    assert match["evidence"] == [{"line": n, "text": f"line {n}"} for n in (4, 7, 12, 30)]


def test_df_reported_per_match():
    edges = [m("docs/a.md", "src/a.py"), m("docs/b.md", "src/a.py"),
             m("docs/a.md", "src/b.py", line=2)]
    matches = rank(edges, ["src/a.py", "src/b.py"], 2)[0]["matches"]
    assert {x["path"]: x["df"] for x in matches} == {"src/a.py": 2, "src/b.py": 1}
    assert set(matches[0]) == {"path", "target", "target_type", "kind", "match", "df",
                               "contribution", "evidence"}


def test_no_edges_and_empty_query_return_empty():
    assert rank([], ["src/a.py"], 0) == []
    assert rank([m("docs/a.md", "src/a.py")], [], 1) == []


def test_ranking_does_not_depend_on_edge_or_path_order():
    edges = [m("docs/a.md", "src/a.py"), m("docs/b.md", "src/", "dir"),
             m("docs/b.md", "src/*.py", "glob", line=2), m("docs/c.md", "src/b.py")]
    paths = ["src/a.py", "src/b.py"]
    one = rank(edges, paths, 3, top=None)
    two = rank(list(reversed(edges)), list(reversed(paths)), 3, top=None)
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


def test_is_matched_needs_an_exact_glob_or_direct_parent_match():
    index = ImpactIndex([m("docs/a.md", "src/", "dir"), m("docs/a.md", "lib/*.c", "glob"),
                         m("docs/a.md", "tools/gen.py")], 1)
    assert index.is_matched("tools/gen.py", ("mention",))
    assert index.is_matched("lib/x.c", ("mention",))
    assert index.is_matched("src/a.py", ("mention",))
    assert not index.is_matched("src/deep/a.py", ("mention",))     # ranked, but not "matched"
    assert not index.is_matched("other/a.py", ("mention",))
    assert index.is_matched("tools/", ("mention",))
    assert not index.is_matched("tools/gen.py", ("cochange",))


# -- path normalisation ----------------------------------------------------

def test_normalise_query_paths(tmp_path):
    root = tmp_path / "proj"
    (root / "src" / "parser").mkdir(parents=True)
    (root / "src" / "parser" / "frame.py").write_text("x")
    (root / "src" / "top.py").write_text("x")
    paths, skipped = normalise_query_paths(root, root, [
        "src/parser/frame.py", "./src/top.py", "src/parser", "src/gone.py",
        str(root / "src" / "top.py"), "../elsewhere/x.py", str(tmp_path / "x.py"), ".", ""])
    assert paths == ["src/gone.py", "src/parser/", "src/parser/frame.py", "src/top.py"]
    assert skipped == [{"path": ".", "reason": "repo_root"},
                       {"path": "../elsewhere/x.py", "reason": "outside_repo"},
                       {"path": str(tmp_path / "x.py"), "reason": "outside_repo"}]


def test_paths_relative_to_cwd_are_normalised(tmp_path):
    root = tmp_path / "proj"
    (root / "src" / "parser").mkdir(parents=True)
    (root / "src" / "parser" / "frame.py").write_text("x")
    cwd = root / "src" / "parser"
    assert normalise_query_paths(root, cwd, ["frame.py"])[0] == ["src/parser/frame.py"]
    assert normalise_query_paths(root, cwd, ["src/parser/frame.py"])[0] == ["src/parser/frame.py"]
    assert normalise_query_paths(root, cwd, ["../top.py"])[0] == ["src/top.py"]
    # Neither reading exists: a deleted path is taken as repo-relative.
    assert normalise_query_paths(root, cwd, ["lib/gone.py"])[0] == ["lib/gone.py"]


# -- run_impact over a tree -----------------------------------------------

def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def fill(root: Path) -> Path:
    write(root, "README.md", "Start with `docs/parser.md`.\n")
    write(root, "docs/parser.md",
          "# Parser\n\nThe framing rules live in `src/parser/frame.py`.\n"
          "See [frame.py](../src/parser/frame.py) for the state table.\n")
    write(root, "docs/overview.md", "Everything under `src/parser/` is covered here.\n")
    write(root, "src/parser/frame.py", "x = 1\n")
    write(root, "src/parser/checksum.py", "y = 2\n")
    write(root, "tools/gen_tables.py", "z = 3\n")
    return root


@pytest.fixture
def tree(tmp_path):
    return fill(tmp_path / "proj")


def test_run_impact_builds_then_queries(tree):
    assert not store_path(tree).exists()
    out = run_impact(tree, {}, paths=["src/parser/frame.py"])
    assert store_path(tree).is_file()
    assert [r["doc"] for r in out["results"]] == ["docs/parser.md", "docs/overview.md"]
    assert out["store"]["built"] is True and out["store"]["docs"] == 3
    first = out["results"][0]["matches"][0]
    assert first["match"] == "exact" and [e["line"] for e in first["evidence"]] == [3, 4]
    assert out["unmatched_paths"] == [] and out["skipped_paths"] == []


def test_no_build_uses_existing_store_and_errors_clearly_when_absent(tree):
    with pytest.raises(ImpactError, match="carta graph build"):
        run_impact(tree, {}, paths=["src/parser/frame.py"], build=False)
    assert not store_path(tree).exists()
    run_impact(tree, {}, paths=["src/parser/frame.py"])
    write(tree, "docs/new.md", "Also `src/parser/frame.py`.\n")
    stale = run_impact(tree, {}, paths=["src/parser/frame.py"], build=False)
    assert stale["store"]["built"] is False
    assert "docs/new.md" not in [r["doc"] for r in stale["results"]]
    fresh = run_impact(tree, {}, paths=["src/parser/frame.py"])
    assert "docs/new.md" in [r["doc"] for r in fresh["results"]]


def test_path_outside_repo_is_reported_in_skipped(tree):
    out = run_impact(tree, {}, paths=["../x.py", "src/parser/frame.py"])
    assert out["query"]["paths"] == ["src/parser/frame.py"]
    assert out["skipped_paths"] == [{"path": "../x.py", "reason": "outside_repo"}]


def test_unmatched_paths_listed(tree):
    out = run_impact(tree, {}, paths=["tools/gen_tables.py", "src/parser/checksum.py"])
    assert out["unmatched_paths"] == ["tools/gen_tables.py"]
    assert [r["doc"] for r in out["results"]] == ["docs/overview.md"]
    assert out["results"][0]["matches"][0]["match"] == "dir"


def test_requires_exactly_one_of_paths_or_diff(tree):
    with pytest.raises(ImpactError):
        run_impact(tree, {})
    with pytest.raises(ImpactError, match="not both"):
        run_impact(tree, {}, paths=["src/a.py"], diff="HEAD~1..HEAD")
    with pytest.raises(ImpactError, match="unknown edge kind"):
        run_impact(tree, {}, paths=["src/a.py"], kinds=["nope"])
    with pytest.raises(ImpactError, match="disabled"):
        run_impact(tree, {"graph": {"enabled": False}}, paths=["src/a.py"])


def test_top_kinds_and_min_score_default_from_config_and_can_be_overridden(tree):
    cfg = {"graph": {"impact": {"top": 1}}}
    out = run_impact(tree, cfg, paths=["src/parser/frame.py"])
    assert len(out["results"]) == 1 and out["query"]["top"] == 1
    out = run_impact(tree, cfg, paths=["src/parser/frame.py"], top=5, min_score=1.0,
                     kinds=["mention"])
    assert out["query"] == {"paths": ["src/parser/frame.py"], "diff": None,
                            "kinds": ["mention"], "top": 5, "min_score": 1.0}
    assert [r["doc"] for r in out["results"]] == ["docs/parser.md"]


def test_json_shape_is_exactly_the_spec(tree):
    out = run_impact(tree, {}, paths=["src/parser/frame.py", "tools/gen_tables.py"])
    assert set(out) == {"schema", "query", "store", "matched_docs", "results",
                        "unmatched_paths", "skipped_paths"}
    assert out["schema"] == 1
    assert set(out["query"]) == {"paths", "diff", "kinds", "top", "min_score"}
    assert set(out["store"]) == {"docs", "edges", "digest", "built"}
    assert set(out["results"][0]) == {"rank", "doc", "score", "in_diff", "matches"}
    assert set(out["results"][0]["matches"][0]) == {
        "path", "target", "target_type", "kind", "match", "df", "contribution", "evidence"}
    assert set(out["results"][0]["matches"][0]["evidence"][0]) == {"line", "text"}
    assert out["query"]["diff"] is None
    assert out["results"][0]["score"] == round(out["results"][0]["score"], 6)


def test_result_is_json_serialisable_and_stable(tree, tmp_path):
    one = run_impact(tree, {}, paths=["src/parser/", "tools/gen_tables.py"])
    other = fill(tmp_path / "elsewhere" / "proj")
    two = run_impact(other, {}, paths=["tools/gen_tables.py", "src/parser/"])
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert str(tmp_path) not in json.dumps(one)


def test_format_impact_snapshot(tree):
    out = run_impact(tree, {}, paths=["src/parser/frame.py", "src/parser/checksum.py",
                                      "tools/gen_tables.py", "../x.py"])
    assert format_impact(out) == (
        "2 docs mention 3 paths (3 docs, 4 edges)\n"
        "\n"
        " 1. docs/parser.md  score 2.347200\n"
        "      src/parser/frame.py  exact, named by 1 doc\n"
        "        L3: The framing rules live in `src/parser/frame.py`.\n"
        "        L4: See [frame.py](../src/parser/frame.py) for the state table.\n"
        " 2. docs/overview.md  score 1.173600\n"
        "      src/parser/checksum.py  via src/parser/ (dir), named by 1 doc\n"
        "        L1: Everything under `src/parser/` is covered here.\n"
        "      src/parser/frame.py  via src/parser/ (dir), named by 1 doc\n"
        "        L1: Everything under `src/parser/` is covered here.\n"
        "\n"
        "No doc mentions: tools/gen_tables.py\n"
        "Skipped: ../x.py (outside_repo)"
    )


def test_format_impact_with_no_results(tree):
    out = run_impact(tree, {}, paths=["tools/gen_tables.py"])
    assert format_impact(out) == (
        "No doc mentions the 1 queried path (3 docs, 4 edges)\n"
        "\n"
        "No doc mentions: tools/gen_tables.py"
    )


def test_format_impact_caps_evidence_at_three_lines():
    edges = [m("docs/a.md", "src/a.py", line=n, evidence=f"line {n}") for n in range(1, 6)]
    results = rank(edges, ["src/a.py"], 1)
    text = format_impact({
        "schema": 1, "query": {"paths": ["src/a.py"], "diff": None, "kinds": ["mention"],
                               "top": 10, "min_score": 0.0},
        "store": {"docs": 1, "edges": 5, "digest": "", "built": False},
        "results": [dict(r, in_diff=False) for r in results],
        "unmatched_paths": [], "skipped_paths": []})
    assert "L3: line 3\n        ... 2 more lines" in text and "L4" not in text


# -- diff mode -------------------------------------------------------------

@pytest.fixture
def repo(git_repo):
    fill(git_repo.root)
    git_repo.commit("first", 0)
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.write("src/parser/frame.py", "x = 2\n")
    git_repo.write("docs/overview.md", "Everything under `src/parser/` is covered here. Edited.\n")
    git_repo.write("README.md", "Start with `docs/parser.md` and `docs/overview.md`.\n")
    git_repo.commit("second", 1)
    return git_repo


@requires_git
def test_diff_mode_queries_changed_non_docs_and_marks_in_diff(repo):
    out = run_impact(repo.root, {}, diff="main...HEAD")
    assert out["query"]["paths"] == ["src/parser/frame.py"]
    assert out["query"]["diff"] == "main...HEAD"
    assert out["skipped_paths"] == [{"path": "README.md", "reason": "doc"},
                                    {"path": "docs/overview.md", "reason": "doc"}]
    assert [(r["doc"], r["in_diff"]) for r in out["results"]] == [
        ("docs/parser.md", False), ("docs/overview.md", True)]


@requires_git
def test_include_docs_adds_changed_docs_to_query(repo):
    out = run_impact(repo.root, {}, diff="main...HEAD", include_docs=True)
    assert out["query"]["paths"] == ["README.md", "docs/overview.md", "src/parser/frame.py"]
    assert out["skipped_paths"] == []
    assert ("README.md", True) in [(r["doc"], r["in_diff"]) for r in out["results"]]


@requires_git
def test_bare_diff_uses_default_branch_range(repo):
    out = run_impact(repo.root, {}, diff="")
    assert out["query"]["diff"] == "main...HEAD"
    assert out["query"]["paths"] == ["src/parser/frame.py"]


@requires_git
def test_bad_range_raises_clear_error(repo):
    with pytest.raises(ImpactError, match="not a revision range"):
        run_impact(repo.root, {}, diff="no-such-branch...HEAD")
    with pytest.raises(ImpactError, match="is a path, not a range"):
        run_impact(repo.root, {}, diff="src/parser/frame.py")


@requires_git
def test_diff_with_no_non_doc_changes_is_an_empty_query_not_an_error(repo):
    out = run_impact(repo.root, {}, diff="HEAD..HEAD")
    assert out["query"]["paths"] == [] and out["results"] == []


def test_diff_outside_a_git_repository_is_a_clear_error(tree):
    with pytest.raises(ImpactError, match="git"):
        run_impact(tree, {}, diff="main...HEAD")


# -- review round 2: deletions, and filters that hide everything -------------------

def test_a_missing_directory_target_still_covers_the_files_that_were_under_it():
    edges = [m("docs/a.md", "src/legacy/", "missing"), m("docs/b.md", "src/other.py")]
    out = rank(edges, ["src/legacy/a.py", "src/legacy/sub/b.py"], 4)
    assert [r["doc"] for r in out] == ["docs/a.md"]
    assert {x["match"] for x in out[0]["matches"]} == {"dir"}
    index = ImpactIndex(edges, 4)
    assert index.is_matched("src/legacy/a.py", ["mention"])
    # a missing *file* target is never a parent
    assert rank([m("docs/a.md", "src/legacy", "missing")], ["src/legacy/a.py"], 4) == []


@pytest.fixture
def deletion_repo(git_repo):
    """Four docs naming code four ways (directory, link, suffix, glob and basename)."""
    r = git_repo
    r.write("src/legacy/a.py", "a = 1\n")
    r.write("src/legacy/sub/b.py", "b = 1\n")
    r.write("src/core/engine.py", "e = 1\n")
    r.write("src/keep.py", "k = 1\n")
    r.write("tools/gen.py", "g = 1\n")
    r.write("docs/dirdoc.md", "Everything in `src/legacy/` is old.\n")
    r.write("docs/linkdoc.md", "Tables come from [gen](tools/gen.py).\n")
    r.write("docs/suffixdoc.md", "The loop is `core/engine.py`; run `./../tools/gen.py`.\n")
    r.write("docs/globdoc.md", "See `src/legacy/*.py` and `engine.py`.\n")
    r.write("docs/other.md", "Unrelated: `src/keep.py`.\n")
    r.commit("base", 0)
    return r


DELETED = ["src/core/engine.py", "src/legacy/a.py", "src/legacy/sub/b.py", "tools/gen.py"]
FOUR = ["docs/dirdoc.md", "docs/globdoc.md", "docs/linkdoc.md", "docs/suffixdoc.md"]


@requires_git
def test_diff_that_deletes_the_mentioned_paths_finds_the_same_docs_as_before(deletion_repo):
    r = deletion_repo
    before = run_impact(r.root, {}, paths=DELETED)
    assert sorted(x["doc"] for x in before["results"]) == FOUR
    r.git("checkout", "-q", "-b", "feat")
    r.git("rm", "-q", "-r", "src/legacy", "tools/gen.py", "src/core/engine.py")
    r.commit("remove the old code", 1)
    after = run_impact(r.root, {}, diff="main..feat")
    assert after["query"]["paths"] == DELETED
    assert sorted(x["doc"] for x in after["results"]) == FOUR
    assert after["unmatched_paths"] == before["unmatched_paths"] == ["src/legacy/sub/b.py"]
    # the store on disk is still the working tree's, untouched by the query
    assert after["store"]["digest"] == run_impact(r.root, {}, paths=["src/keep.py"])["store"]["digest"]


@requires_git
def test_querying_a_deleted_path_by_name_finds_the_docs_that_named_it(deletion_repo):
    r = deletion_repo
    r.git("rm", "-q", "-r", "src/legacy", "tools/gen.py", "src/core/engine.py")
    r.commit("remove the old code", 1)
    out = run_impact(r.root, {}, paths=DELETED)
    assert sorted(x["doc"] for x in out["results"]) == FOUR
    assert out["unmatched_paths"] == ["src/legacy/sub/b.py"]      # only a grandparent names it


@requires_git
def test_a_rename_in_the_diff_finds_docs_under_the_old_name(deletion_repo):
    r = deletion_repo
    r.git("checkout", "-q", "-b", "feat")
    r.git("mv", "src/core/engine.py", "src/core/motor.py")
    r.commit("rename", 1)
    out = run_impact(r.root, {}, diff="main..feat")
    assert out["query"]["paths"] == ["src/core/engine.py", "src/core/motor.py"]
    assert sorted(x["doc"] for x in out["results"]) == ["docs/globdoc.md", "docs/suffixdoc.md"]


def test_top_and_min_score_are_validated(tree):
    for top in (0, -1):
        with pytest.raises(ImpactError, match="top"):
            run_impact(tree, {}, paths=["src/parser/frame.py"], top=top)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ImpactError, match="min-score"):
            run_impact(tree, {}, paths=["src/parser/frame.py"], min_score=bad)


def test_empty_result_says_when_the_filter_hid_the_matches(tree):
    out = run_impact(tree, {}, paths=["src/parser/frame.py"], min_score=1000.0)
    assert out["results"] == [] and out["matched_docs"] == 2
    assert format_impact(out).splitlines()[0] == (
        "2 docs mention the 1 queried path, but none scores at least 1000 "
        "(--min-score) (3 docs, 4 edges)")
    out = run_impact(tree, {}, paths=["src/parser/frame.py"], top=1)
    assert out["matched_docs"] == 2
    assert format_impact(out).splitlines()[0] == (
        "Top 1 of 2 docs that mention 1 path (3 docs, 4 edges)")
