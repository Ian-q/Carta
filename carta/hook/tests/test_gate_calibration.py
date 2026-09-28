"""The zone gate's calibrated defaults (#118).

Derived with `python -m carta.hook.eval.calibrate gate` over an 84-query eval corpus plus
15 off-corpus prompts (prompts nothing in the corpus answers — the case an eval set cannot
supply, and the one `low_threshold` exists for). Measured dense-cosine ranges of the TOP hit:

    relevant             0.651 - 0.815  (p25 0.706)
    plausible-but-wrong  0.714 - 0.795
    unrelated            0.616 - 0.784
    no answer exists     0.600 - 0.659  (median 0.619)

low_threshold 0.65 silences 9 of the 10 no-answer prompts that carry a cosine and ZERO
relevant hits; 0.68 starts costing 3 relevant, 0.70 costs 9. agree_rank 1 lets 0 unrelated
and 1 plausible-but-wrong top hit bypass the judge, against 6 and 4 at the old default of 3.
"""
from carta.config import DEFAULTS
from carta.hook.hook import _gate_zone


def _hit(dense, dense_rank, sparse_rank):
    return {"score": dense, "dense_score": dense,
            "lane_ranks": {"dense": dense_rank, "sparse": sparse_rank}}


def test_calibrated_defaults():
    pr = DEFAULTS["proactive_recall"]
    assert pr["low_threshold"] == 0.65
    assert pr["agree_rank"] == 1


def _zone(hit):
    pr = DEFAULTS["proactive_recall"]
    return _gate_zone([hit], agree_rank=pr["agree_rank"], low=pr["low_threshold"],
                      high=pr["high_threshold"])


def test_no_answer_cosine_is_now_silenced():
    """0.619 is the median top-hit cosine when nothing answers the prompt; the old 0.60
    floor let it through to the judge."""
    assert _zone(_hit(0.619, 0, 0)) == "silent"


def test_weakest_observed_relevant_cosine_still_survives():
    """0.651 was the lowest cosine of any RELEVANT top hit — the floor must sit below it."""
    assert _zone(_hit(0.651, 0, 0)) != "silent"


def test_agree_rank_1_requires_the_top_of_both_lanes_to_skip_the_judge():
    assert _zone(_hit(0.73, 0, 0)) == "inject"
    assert _zone(_hit(0.73, 0, 1)) == "judge"      # would have injected at agree_rank 3
    assert _zone(_hit(0.73, 2, 2)) == "judge"


def test_sparse_only_top_hit_is_still_never_silenced():
    """No dense cosine means relevance was never measured; silencing it is the #123 bug."""
    hit = {"score": 0.5, "dense_score": None, "lane_ranks": {"dense": None, "sparse": 0}}
    assert _zone(hit) == "judge"
