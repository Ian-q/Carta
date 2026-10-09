"""The baselines the graph arm has to beat, on hand-built inputs."""
from __future__ import annotations

import math

import pytest

from carta.graph.eval.baselines import (
    BasenameIndex, PopularityWindow, basename_idf_ranking, basename_ranking, basename_tokens,
    popularity_ranking,
)

DOCS = {"CHANGELOG.md", "docs/a.md", "docs/b.md", "docs/c.md"}


def test_popularity_counts_only_the_window_and_only_prior_commits():
    prior = [{"docs/a.md"}, {"docs/a.md"}, {"docs/a.md"},        # outside a window of 3
             {"docs/b.md"}, set(), {"docs/b.md", "docs/c.md"}]
    assert popularity_ranking(prior, DOCS, window=3) == ["docs/b.md", "docs/c.md"]
    assert popularity_ranking(prior, DOCS, window=6)[0] == "docs/a.md"
    assert popularity_ranking(prior, DOCS, window=6, top=1) == ["docs/a.md"]


def test_popularity_drops_docs_absent_at_parent():
    prior = [{"docs/gone.md"}, {"docs/gone.md"}, {"docs/a.md"}]
    assert popularity_ranking(prior, DOCS, window=10) == ["docs/a.md"]


def test_popularity_ties_by_path():
    prior = [{"docs/c.md", "docs/a.md"}, {"docs/b.md"}]
    assert popularity_ranking(prior, DOCS, window=10) == ["docs/a.md", "docs/b.md", "docs/c.md"]


def test_popularity_with_no_history_is_empty():
    assert popularity_ranking([], DOCS, window=10) == []
    assert popularity_ranking([{"docs/a.md"}], DOCS, window=0) == []


def test_popularity_excludes_before_truncating():
    prior = [{"CHANGELOG.md", "docs/a.md"}, {"CHANGELOG.md"}, {"docs/b.md"}]
    assert popularity_ranking(prior, DOCS, window=10, top=1) == ["CHANGELOG.md"]
    assert popularity_ranking(prior, DOCS, window=10, top=1,
                              exclude={"CHANGELOG.md"}) == ["docs/a.md"]


def test_popularity_window_is_incremental():
    win = PopularityWindow(2)
    assert win.ranking(DOCS) == []
    win.observe({"docs/a.md"})
    win.observe({"docs/b.md"})
    assert win.ranking(DOCS) == ["docs/a.md", "docs/b.md"]
    win.observe({"docs/b.md"})                      # pushes the first commit out
    assert win.ranking(DOCS) == ["docs/b.md"]
    assert win.ranking(DOCS, exclude={"docs/b.md"}) == []


def test_basename_tokens_whole_token_only():
    tokens = basename_tokens("see frame.py. Also `src/parser/frame.py`, not subframe.pyc\n"
                             "or frame.py.bak; (Makefile) and é.md and frame.py:12")
    assert tokens["frame.py"] == 3
    assert tokens["subframe.pyc"] == 1 and tokens["frame.py.bak"] == 1
    assert tokens["Makefile"] == 1 and tokens["é.md"] == 1
    assert "frame" not in tokens and "subframe.py" not in tokens


def test_basename_ranking_orders_by_distinct_then_total_then_path():
    doc_tokens = {
        "docs/two.md": {"a.py": 1, "b.py": 1},
        "docs/many.md": {"a.py": 9},
        "docs/few.md": {"a.py": 2},
        "docs/also_few.md": {"b.py": 2},
        "docs/none.md": {"z.py": 4},
    }
    changed = ["src/a.py", "lib/b.py", "other/a.py"]
    assert basename_ranking(changed, doc_tokens) == [
        "docs/two.md", "docs/many.md", "docs/also_few.md", "docs/few.md"]
    assert basename_ranking(changed, doc_tokens, top=2) == ["docs/two.md", "docs/many.md"]
    assert basename_ranking(changed, doc_tokens, top=1, exclude={"docs/two.md"}) == [
        "docs/many.md"]


def test_basename_is_case_sensitive():
    assert basename_ranking(["src/Frame.py"], {"d.md": {"frame.py": 1}}) == []


def test_basename_ignores_directory_part():
    doc_tokens = {"d.md": basename_tokens("the `parser` package"), "e.md": {"frame.py": 1}}
    assert basename_ranking(["parser/frame.py"], doc_tokens) == ["e.md"]


def test_basename_no_match_is_empty():
    assert basename_ranking(["src/a.py"], {"d.md": {"b.py": 1}}) == []
    assert basename_ranking([], {"d.md": {"b.py": 1}}) == []
    assert basename_idf_ranking([], {"d.md": {"b.py": 1}}, n_docs=1) == []


def test_basename_idf_weights_rare_names_and_long_docs_do_not_win():
    # common.py is in every doc, rare.py in one. Plain basename prefers the doc
    # that repeats the common name; the idf form prefers the rare match.
    doc_tokens = {
        "docs/long.md": {"common.py": 2},
        "docs/short.md": {"rare.py": 1},
        "docs/x.md": {"common.py": 1}, "docs/y.md": {"common.py": 1},
    }
    changed = ["src/common.py", "src/rare.py"]
    assert basename_ranking(changed, doc_tokens)[0] == "docs/long.md"
    ranked = basename_idf_ranking(changed, doc_tokens, n_docs=4, with_scores=True)
    assert [doc for doc, _ in ranked][:2] == ["docs/short.md", "docs/long.md"]
    scores = dict(ranked)
    assert scores["docs/short.md"] == pytest.approx(math.log(1 + 4 / 1) * 1.0)
    assert scores["docs/long.md"] == pytest.approx(math.log(1 + 4 / 3) * (1 + math.log(2)))


def test_basename_index_matches_the_pure_functions_and_is_incremental():
    index = BasenameIndex(universe={"a.py", "b.py"})
    index.set_doc("d1.md", {"a.py": 2, "noise": 5})
    index.set_doc("d2.md", {"b.py": 1, "a.py": 1})
    index.set_doc("d3.md", {"noise": 1})
    assert index.df("a.py") == 2 and index.df("noise") == 0      # outside the universe
    changed = ["x/a.py", "y/b.py"]
    plain = {"d1.md": {"a.py": 2}, "d2.md": {"b.py": 1, "a.py": 1}}
    assert index.rank(changed) == basename_ranking(changed, plain)
    assert index.rank_idf(changed, n_docs=3) == basename_idf_ranking(changed, plain, n_docs=3)
    index.set_doc("d2.md", {"a.py": 1})             # replaced: b.py is gone from d2
    assert index.df("b.py") == 0
    index.remove_doc("d1.md")
    assert index.rank(changed) == ["d2.md"]
    index.remove_doc("never-there.md")
