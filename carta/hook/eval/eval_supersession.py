"""Supersession-judge precision/recall eval over a labeled corpus.

The pure `evaluate()` is unit-tested with a fake judge. `main()` runs the real
`_stale_judge` against live Ollama (the configured model) and is the manual
validation gate for the evidence-citation judge (#84). Run from any Carta repo:
    python -m carta.hook.eval.eval_supersession
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml

CASES_PATH = Path(__file__).parent / "supersession_cases.yaml"


def evaluate(cases: list[dict], judge_fn) -> dict:
    """Run judge_fn(section_text, candidate{source,excerpt}) over labeled cases.

    label is 'true_positive' (judge should say conflict) or 'false_positive'
    (judge should not). A None verdict is counted as an error (not a pass)."""
    tp = fp = tn = fn = errors = 0
    rows = []
    for c in cases:
        verdict = judge_fn(c["section_text"], {"source": c["source"], "excerpt": c["candidate_excerpt"]})
        is_pos = c["label"] == "true_positive"
        if verdict is None:
            errors += 1
            outcome = "ERROR"
        elif verdict and is_pos:
            tp += 1
            outcome = "TP ok"
        elif verdict and not is_pos:
            fp += 1
            outcome = "FP BAD"
        elif not verdict and is_pos:
            fn += 1
            outcome = "FN BAD"
        else:
            tn += 1
            outcome = "TN ok"
        rows.append({"id": c["id"], "label": c["label"], "verdict": verdict, "outcome": outcome})
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "errors": errors,
            "precision": round(precision, 3), "recall": round(recall, 3), "rows": rows}


def main() -> None:
    from carta.config import load_config, find_config
    from carta.hook.stale_scan import _stale_judge

    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cfg = load_config(find_config())
    started = time.time()
    res = evaluate(cases, lambda s, c: _stale_judge(s, c, cfg))
    elapsed = time.time() - started

    for r in res["rows"]:
        print(f"  {r['outcome']:7} {r['id']}  (verdict={r['verdict']})")
    print(f"\nTP={res['tp']} FP={res['fp']} TN={res['tn']} FN={res['fn']} errors={res['errors']}")
    print(f"precision={res['precision']} recall={res['recall']} "
          f"elapsed={elapsed:.1f}s ({elapsed / max(1, len(cases)):.1f}s/case)")
    ok = res["fp"] == 0 and res["fn"] == 0 and res["errors"] == 0
    print("RESULT:", "PASS — all cases correct" if ok else "FAIL — see rows above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
