"""Pure parts of the judge calibration tool (carta/hook/eval/calibrate.py)."""
import math

import pytest

from carta.hook.eval import calibrate


def test_auroc_perfect_and_inverted_separation():
    assert calibrate.auroc([1.0, 2.0], [-1.0, 0.0]) == 1.0
    assert calibrate.auroc([-1.0, 0.0], [1.0, 2.0]) == 0.0


def test_auroc_counts_ties_as_half():
    assert calibrate.auroc([1.0], [1.0]) == 0.5


def test_auroc_is_nan_without_both_classes():
    assert math.isnan(calibrate.auroc([1.0], []))


def test_sweep_picks_the_threshold_that_separates():
    scores = [5.0, 4.0, -5.0, -4.0]
    kinds = ["pos", "pos", "easy", "easy"]
    r = calibrate.sweep(scores, kinds)
    assert r["auc_vs_easy"] == 1.0
    assert r["youden"]["tpr"] == 1.0 and r["youden"]["fpr_easy"] == 0.0
    assert -5.0 < r["youden"]["threshold"] <= 4.0


def test_sweep_low_noise_point_respects_the_easy_fpr_budget():
    scores = [1.0, 0.5, 0.9, -3.0]
    kinds = ["pos", "pos", "easy", "easy"]      # one easy negative scores 0.9
    r = calibrate.sweep(scores, kinds, max_easy_fpr=0.0)
    assert r["max_easy_fpr_0.0"]["threshold"] > 0.9      # must exclude that negative
    assert r["max_easy_fpr_0.0"]["fpr_easy"] == 0.0


def test_build_pairs_labels_positive_easy_and_hard():
    chunks = [
        {"file_path": "docs/hw/monitor-rev-b.md", "text": "The max sense voltage is 163.84 mV.", "live": True},
        {"file_path": "docs/hw/monitor-rev-a.md", "text": "sense voltage ranges are listed per part " * 5, "live": True},
        {"file_path": "docs/biz/pricing.md", "text": "Fleet payback lands inside three years. " * 6, "live": True},
    ]
    queries = [{"q": "what is the max sense voltage of the newer monitor",
                "expect": ["monitor-rev-b"], "reject": ["monitor-rev-a"],
                "evidence": {"file": "docs/hw/monitor-rev-b.md", "quote": "The max sense voltage is 163.84 mV."}}]
    kinds = [p["kind"] for p in calibrate.build_pairs(queries, chunks)]
    assert kinds.count("pos") == 1 and kinds.count("easy") == 1 and kinds.count("hard") == 1


def test_build_pairs_skips_a_query_whose_evidence_is_not_in_the_index():
    """Eval set and index disagreeing is a data problem — such a pair would measure it."""
    chunks = [{"file_path": "docs/a.md", "text": "something else entirely", "live": True}]
    queries = [{"q": "q", "expect": ["a"], "evidence": {"file": "docs/a.md", "quote": "not present here"}}]
    assert calibrate.build_pairs(queries, chunks) == []


def test_build_pairs_ignores_dead_chunks():
    chunks = [{"file_path": "docs/a.md", "text": "the answer is 42 and nothing else", "live": False}]
    queries = [{"q": "q", "expect": ["a"], "evidence": {"file": "docs/a.md", "quote": "the answer is 42"}}]
    assert calibrate.build_pairs(queries, chunks) == []


def test_build_pairs_is_deterministic_for_a_seed():
    chunks = [{"file_path": "docs/a/x.md", "text": "the answer is 42 and nothing else", "live": True}] + [
        {"file_path": f"docs/b/{i}.md", "text": f"unrelated text number {i} " * 20, "live": True} for i in range(8)]
    queries = [{"q": "q", "expect": ["x"], "evidence": {"file": "docs/a/x.md", "quote": "the answer is 42"}}]
    a = calibrate.build_pairs(queries, chunks, seed=7)
    b = calibrate.build_pairs(queries, chunks, seed=7)
    assert [p["text"] for p in a] == [p["text"] for p in b]
