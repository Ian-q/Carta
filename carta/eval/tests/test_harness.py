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
