import pytest

from carta.eval.harness import load_eval_set, compute_metrics, EvalQuery


def test_load_eval_set(tmp_path):
    p = tmp_path / "set.yaml"
    p.write_text(
        "queries:\n"
        "  - q: what baud rate is the serial bridge\n"
        "    expect: [serial-bridge, CLAUDE.md]\n"
        "  - q: load cell counts per pound\n"
        "    expect: [bench-measurements]\n"
    )
    qs = load_eval_set(p)
    assert len(qs) == 2
    assert qs[0] == EvalQuery(q="what baud rate is the serial bridge",
                              expect=["serial-bridge", "CLAUDE.md"])


def test_load_eval_set_rejects_blank_expectations(tmp_path):
    """An empty-string expect is a substring of every path (guaranteed HIT) and an
    empty expect list is a guaranteed MISS — both silently corrupt recall/MRR, so
    load_eval_set must reject them loudly (audit CA-26)."""
    import yaml
    p = tmp_path / "set.yaml"
    p.write_text(yaml.dump({"queries": [{"q": "x", "expect": [""]}]}))
    with pytest.raises(ValueError):
        load_eval_set(p)
    p.write_text(yaml.dump({"queries": [{"q": "y", "expect": []}]}))
    with pytest.raises(ValueError):
        load_eval_set(p)


def test_load_eval_set_strips_blank_but_keeps_real_expectations(tmp_path):
    """A blank entry mixed with a real one is dropped, not fatal."""
    import yaml
    p = tmp_path / "set.yaml"
    p.write_text(yaml.dump({"queries": [{"q": "x", "expect": ["  ", "real.md"]}]}))
    qs = load_eval_set(p)
    assert qs[0].expect == ["real.md"]


def test_compute_metrics_hit_and_miss():
    eval_queries = [
        EvalQuery(q="A", expect=["alpha"]),
        EvalQuery(q="B", expect=["zeta"]),
    ]
    results_per_query = [
        ["docs/other.md", "docs/alpha-spec.md", "docs/x.md"],  # hit at rank 2
        ["docs/p.md", "docs/q.md"],                            # no hit
    ]
    m = compute_metrics(eval_queries, results_per_query, k=3)
    assert m["n_queries"] == 2
    assert m["recall_at_k"] == 0.5
    assert m["mrr"] == 0.25
    assert m["per_query"][0]["first_hit_rank"] == 2
    assert m["per_query"][1]["first_hit_rank"] is None


def test_compute_metrics_respects_k_cutoff():
    eval_queries = [EvalQuery(q="A", expect=["alpha"])]
    results_per_query = [["x.md", "y.md", "alpha.md"]]  # hit at rank 3
    assert compute_metrics(eval_queries, results_per_query, k=2)["recall_at_k"] == 0.0
    assert compute_metrics(eval_queries, results_per_query, k=3)["recall_at_k"] == 1.0


def test_compute_metrics_rejects_length_mismatch():
    with pytest.raises(ValueError):
        compute_metrics([EvalQuery(q="A", expect=["x"])], [], k=5)


def test_compute_metrics_empty_eval_set():
    m = compute_metrics([], [], k=5)
    assert m["n_queries"] == 0
    assert m["recall_at_k"] == 0.0
    assert m["mrr"] == 0.0


def test_load_eval_set_rejects_query_missing_q(tmp_path):
    """A row with valid expectations but no 'q' must fail with a clear ValueError,
    not a raw KeyError."""
    import yaml
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.dump({"queries": [{"expect": ["docs/a.md"]}]}))
    with pytest.raises(ValueError, match="no 'q'"):
        load_eval_set(p)


# ---------------------------------------------------------------------------
# reject / tags / multi-cutoff recall (eval-set v2: de-saturation, #19)
# ---------------------------------------------------------------------------

def test_load_eval_set_parses_reject_and_tags(tmp_path):
    import yaml
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.dump({"queries": [
        {"q": "shunt limit", "expect": ["rev-b"], "reject": ["rev-a"], "tags": ["hard-negative"]},
        {"q": "plain", "expect": ["a.md"]},
    ]}))
    qs = load_eval_set(p)
    assert qs[0].reject == ["rev-a"]
    assert qs[0].tags == ["hard-negative"]
    assert qs[1].reject == [] and qs[1].tags == []


def test_load_eval_set_drops_blank_reject_entries(tmp_path):
    """A blank reject is a substring of every path — it would flag every query as a
    violation, the mirror image of the blank-expect bug (CA-26)."""
    import yaml
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.dump({"queries": [{"q": "x", "expect": ["a.md"], "reject": ["", "  ", "b.md"]}]}))
    assert load_eval_set(p)[0].reject == ["b.md"]


def test_compute_metrics_reports_recall_at_each_cutoff():
    qs = [EvalQuery(q=c, expect=[c]) for c in ("a", "b", "c")]
    results = [["a.md"], ["x.md", "y.md", "b.md"], ["x.md"]]  # ranks 1, 3, miss
    m = compute_metrics(qs, results, k=5)
    assert m["recall_at"] == {1: pytest.approx(1 / 3), 3: pytest.approx(2 / 3), 5: pytest.approx(2 / 3)}
    assert m["recall_at_k"] == pytest.approx(2 / 3)  # unchanged key kept for callers


def test_recall_at_omits_cutoffs_deeper_than_k():
    m = compute_metrics([EvalQuery(q="a", expect=["a"])], [["x.md", "a.md"]], k=2)
    assert set(m["recall_at"]) == {1, 2}


def test_reject_ranked_above_gold_is_a_violation():
    qs = [EvalQuery(q="q", expect=["rev-b"], reject=["rev-a"])]
    m = compute_metrics(qs, [["current-monitor-rev-a.pdf", "current-monitor-rev-b.pdf"]], k=5)
    row = m["per_query"][0]
    assert row["first_hit_rank"] == 2
    assert row["first_reject_rank"] == 1
    assert row["reject_violation"] is True
    assert m["reject_violations"] == 1
    assert m["n_with_reject"] == 1


def test_reject_below_gold_is_not_a_violation():
    qs = [EvalQuery(q="q", expect=["rev-b"], reject=["rev-a"])]
    m = compute_metrics(qs, [["current-monitor-rev-b.pdf", "current-monitor-rev-a.pdf"]], k=5)
    assert m["per_query"][0]["reject_violation"] is False
    assert m["reject_violations"] == 0


def test_reject_in_top_k_with_gold_missing_is_a_violation():
    qs = [EvalQuery(q="q", expect=["rev-b"], reject=["rev-a"])]
    m = compute_metrics(qs, [["x.md", "current-monitor-rev-a.pdf"]], k=5)
    assert m["per_query"][0]["reject_violation"] is True


def test_reject_beyond_k_is_not_a_violation():
    qs = [EvalQuery(q="q", expect=["rev-b"], reject=["rev-a"])]
    m = compute_metrics(qs, [["x.md", "y.md", "current-monitor-rev-a.pdf"]], k=2)
    assert m["per_query"][0]["first_reject_rank"] is None
    assert m["per_query"][0]["reject_violation"] is False


def test_path_matching_expect_never_counts_as_reject():
    """expect wins: 'monitor-rev' as a reject must not fire on the gold 'monitor-rev-b-errata' path."""
    qs = [EvalQuery(q="q", expect=["monitor-rev-b-errata"], reject=["monitor-rev"])]
    m = compute_metrics(qs, [["monitor-rev-b-errata.pdf"]], k=5)
    assert m["per_query"][0]["first_reject_rank"] is None
    assert m["per_query"][0]["reject_violation"] is False


def test_queries_without_reject_report_no_violation_field_noise():
    m = compute_metrics([EvalQuery(q="a", expect=["a"])], [["x.md"]], k=5)
    assert m["per_query"][0]["reject_violation"] is False
    assert m["n_with_reject"] == 0 and m["reject_violations"] == 0


def test_by_tag_breakdown():
    qs = [
        EvalQuery(q="a", expect=["a"], tags=["pdf-deep", "vocab-mismatch"]),
        EvalQuery(q="b", expect=["b"], tags=["pdf-deep"]),
        EvalQuery(q="c", expect=["c"]),
    ]
    m = compute_metrics(qs, [["a.md"], ["x.md"], ["y.md", "c.md"]], k=5)
    assert m["by_tag"]["pdf-deep"] == {"n": 2, "recall_at_k": 0.5, "mrr": 0.5}
    assert m["by_tag"]["vocab-mismatch"] == {"n": 1, "recall_at_k": 1.0, "mrr": 1.0}
    assert "c" not in m["by_tag"] and set(m["by_tag"]) == {"pdf-deep", "vocab-mismatch"}


def test_scalar_fields_are_one_entry_not_split_into_characters(tmp_path):
    """YAML's scalar form (`reject: foo`) must mean ['foo'] — iterating a str split it into
    characters, and a one-letter reject/expect matches nearly every path."""
    import yaml
    p = tmp_path / "eval.yaml"
    p.write_text(yaml.dump({"queries": [
        {"q": "x", "expect": "current-monitor-rev-b", "reject": "current-monitor-rev-a", "tags": "pdf-deep"},
    ]}))
    q = load_eval_set(p)[0]
    assert q.expect == ["current-monitor-rev-b"]
    assert q.reject == ["current-monitor-rev-a"]
    assert q.tags == ["pdf-deep"]
