from carta.hook.eval import eval_supersession as ev


CASES = [
    {"id": "tp1", "label": "true_positive", "source": "a", "section_text": "s", "candidate_excerpt": "e"},
    {"id": "tp2", "label": "true_positive", "source": "a", "section_text": "s", "candidate_excerpt": "e"},
    {"id": "fp1", "label": "false_positive", "source": "a", "section_text": "s", "candidate_excerpt": "e"},
    {"id": "fp2", "label": "false_positive", "source": "a", "section_text": "s", "candidate_excerpt": "e"},
]


def test_evaluate_perfect_judge():
    # CASES order is tp1, tp2, fp1, fp2 — a perfect judge says True, True, False, False
    verdicts = iter([True, True, False, False])
    res = ev.evaluate(CASES, lambda s, c: next(verdicts))
    assert res["tp"] == 2 and res["fp"] == 0 and res["fn"] == 0 and res["tn"] == 2
    assert res["precision"] == 1.0 and res["recall"] == 1.0


def test_evaluate_counts_fp_fn_and_errors():
    verdicts = iter([False, None, True, False])  # tp1->FN, tp2->ERROR, fp1->FP, fp2->TN
    res = ev.evaluate(CASES, lambda s, c: next(verdicts))
    assert res["fn"] == 1 and res["errors"] == 1 and res["fp"] == 1 and res["tn"] == 1
    assert res["tp"] == 0
