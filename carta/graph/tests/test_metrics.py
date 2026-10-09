"""Ranking metrics and the seeded bootstrap, on hand-computed cases."""
from __future__ import annotations

import pytest

from carta.graph.eval.metrics import (
    aggregate, bootstrap_ci, paired_bootstrap, score_ranking,
)

KS = (1, 3, 5, 10)


def test_perfect_ranking():
    row = score_ranking(["a.md"], {"a.md"}, KS)
    assert row["recall_at"] == {1: 1.0, 3: 1.0, 5: 1.0, 10: 1.0}
    assert row["precision_at"] == {1: 1.0, 3: 1 / 3, 5: 0.2, 10: 0.1}
    assert row["any_hit_at"] == {1: 1, 3: 1, 5: 1, 10: 1}
    assert row["rr"] == 1.0 and row["first_rank"] == 1 and row["returned"] == 1


def test_gold_at_rank_3():
    row = score_ranking(["x.md", "y.md", "a.md", "z.md"], {"a.md"}, KS)
    assert row["recall_at"][1] == 0.0 and row["recall_at"][3] == 1.0
    assert row["precision_at"][3] == pytest.approx(1 / 3)
    assert row["any_hit_at"][1] == 0 and row["any_hit_at"][3] == 1
    assert row["rr"] == pytest.approx(1 / 3) and row["first_rank"] == 3


def test_two_gold_one_found():
    row = score_ranking(["a.md", "x.md"], {"a.md", "b.md"}, KS)
    assert row["recall_at"][5] == 0.5
    assert row["precision_at"][1] == 1.0


def test_precision_divides_by_k_not_returned():
    row = score_ranking(["a.md", "b.md"], {"a.md", "b.md"}, KS)
    assert row["precision_at"][5] == pytest.approx(0.4)
    assert row["precision_at"][10] == pytest.approx(0.2)
    assert row["recall_at"][5] == 1.0


def test_empty_ranking():
    row = score_ranking([], {"a.md"}, KS)
    assert set(row["recall_at"].values()) == {0.0}
    assert row["rr"] == 0.0 and row["first_rank"] is None and row["returned"] == 0


def test_rr_reads_the_whole_ranking_and_returned_is_capped():
    """MRR must not move with the cutoffs asked for, so it is not truncated at max(k)."""
    ranked = [f"x{i}.md" for i in range(10)] + ["a.md"]
    row = score_ranking(ranked, {"a.md"}, KS)
    assert row["rr"] == pytest.approx(1 / 11) and row["first_rank"] == 11
    assert row["returned"] == 10 and row["any_hit_at"][10] == 0
    assert score_ranking(ranked, {"a.md"}, (1,))["rr"] == row["rr"]


def test_duplicate_docs_in_ranking_are_an_error():
    with pytest.raises(ValueError):
        score_ranking(["a.md", "a.md"], {"a.md"}, KS)


def test_empty_gold_is_an_error():
    with pytest.raises(ValueError):
        score_ranking(["a.md"], set(), KS)


def test_aggregate_macro_average():
    rows = [score_ranking(["a.md"], {"a.md"}, KS),
            score_ranking(["x.md", "a.md"], {"a.md", "b.md"}, KS),
            score_ranking([], {"a.md"}, KS)]
    agg = aggregate(rows, KS)
    assert agg["n"] == 3
    assert agg["recall_at"]["1"] == pytest.approx(1 / 3)
    assert agg["recall_at"]["3"] == pytest.approx((1 + 0.5 + 0) / 3)
    assert agg["any_hit_at"]["3"] == pytest.approx(2 / 3)
    assert agg["precision_at"]["5"] == pytest.approx((0.2 + 0.2 + 0) / 3)
    assert agg["mrr"] == pytest.approx((1 + 0.5 + 0) / 3)
    assert agg["mean_returned"] == pytest.approx(1.0)


def test_aggregate_empty_population_is_zeros_with_n_zero():
    agg = aggregate([], KS)
    assert agg["n"] == 0 and agg["mrr"] == 0.0
    assert agg["recall_at"] == {"1": 0.0, "3": 0.0, "5": 0.0, "10": 0.0}


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    values = [0.0, 1.0, 1.0, 0.5, 0.0, 1.0, 0.25, 0.0, 1.0, 1.0] * 4
    lo, hi = bootstrap_ci(values)
    assert (lo, hi) == bootstrap_ci(values)
    mean = sum(values) / len(values)
    assert lo < mean < hi
    assert 0.0 <= lo and hi <= 1.0
    assert bootstrap_ci([]) == (0.0, 0.0)
    assert bootstrap_ci([0.5, 0.5, 0.5]) == (0.5, 0.5)


def test_paired_bootstrap_counts_and_sign():
    a = [1.0, 1.0, 0.5, 0.0, 1.0, 1.0, 1.0, 0.5] * 5
    b = [0.0, 1.0, 0.0, 0.0, 0.5, 0.0, 1.0, 0.0] * 5
    out = paired_bootstrap(a, b)
    assert out["delta"] == pytest.approx(sum(a) / len(a) - sum(b) / len(b))
    assert (out["better"], out["worse"], out["tied"]) == (25, 0, 15)
    lo, hi = out["ci95"]
    assert 0.0 < lo < out["delta"] < hi
    flipped = paired_bootstrap(b, a)
    assert flipped["delta"] == pytest.approx(-out["delta"])
    assert flipped["ci95"][1] < 0.0 and flipped["worse"] == 25
    assert paired_bootstrap([], []) == {
        "delta": 0.0, "ci95": [0.0, 0.0], "better": 0, "worse": 0, "tied": 0}


def test_paired_requires_equal_lengths():
    with pytest.raises(ValueError):
        paired_bootstrap([1.0], [1.0, 0.0])


def test_bootstrap_equals_the_plain_definition_and_the_streamed_path(monkeypatch):
    import random

    from carta.graph.eval import metrics
    values = [((i * 37) % 11) / 10 for i in range(53)]
    rng = random.Random(0)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(400))
    expected = (means[int(0.025 * 400)], means[int(0.975 * 400) - 1])
    got = bootstrap_ci(values, resamples=400)
    assert got == pytest.approx(expected, abs=1e-12)
    monkeypatch.setattr(metrics, "_SHARED_LIMIT", 0)            # force the streamed path
    assert bootstrap_ci(values, resamples=400) == got
