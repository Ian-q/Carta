# Supersession Precision Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the yes/no supersession judge with an evidence-citation gate that affirms "superseded" only on a genuine clause-level contradiction, fixing the ~0% precision (#84) without losing real true-positives.

**Architecture:** A new `ollama_json` helper gets structured JSON from a capable local judge; `_stale_judge` (shared by claude-md-sync and the stale-reference hook) is rewritten to ask the judge to quote the contradicting clause + the stale claim and return `conflict: true/false`; the default judge model moves to `qwen3.5:9b`. A committed 11-case labeled eval corpus (already present) + a runner measure precision/recall as the validation gate.

**Tech Stack:** Python 3.10+, `requests` (Ollama HTTP), PyYAML, pytest. Reuses `run_stale_scan` and its `bool | None` `judge_fn` contract.

## Global Constraints

- Python 3.10+; type hints on signatures; `snake_case` functions; 4-space indent; match surrounding style.
- **Fail-open everywhere:** the judge returns `None` on any network/parse/timeout/format error; `run_stale_scan` treats `None` as "not flagged" and counts it in `judge_errors` (already shipped). No new crash path; never a false "stale" on judge failure.
- **`judge_fn` contract is `(section_text: str, candidate: dict) -> bool | None`** — unchanged, so `run_stale_scan` and its injectable tests stay intact. `candidate` has keys `source` and `excerpt`.
- **Scope:** judge precision only. Do NOT change retrieval, `run_stale_scan`'s loop, or add structural guards — those are deferred per the spec.
- Default judge model for `hooks.stale_scan`: **`qwen3.5:9b`** (the recall hook keeps `qwen3.5:0.8b` — do not touch `proactive_recall`/`cross_project_recall`). Default `judge_timeout_s`: **30**.
- TDD: failing test first, minimal implementation, frequent commits. Unit tests mock Ollama; the live eval (Task 5) is the only step needing a running judge.

---

## File Structure

- `carta/hook/judge.py` — **modify**: add `ollama_json` + `_extract_json` (keep `ollama_yesno`, still used by the recall hook).
- `carta/hook/stale_scan.py` — **modify**: rewrite `_stale_judge` to the evidence-citation gate; import `ollama_json`.
- `carta/config.py` — **modify**: `hooks.stale_scan` defaults `ollama_model` → `qwen3.5:9b`, `judge_timeout_s` → `30`.
- `carta/hook/eval/eval_supersession.py` — **create**: `evaluate(cases, judge_fn)` pure function + `main()` live runner.
- `carta/hook/eval/supersession_cases.yaml` — **already committed** (11 labeled cases; do not recreate).
- `carta/hook/eval/__init__.py` — **already committed** (empty package marker).
- `carta/hook/tests/test_judge.py` — **create**.
- `carta/hook/tests/test_stale_scan.py` — **modify**: judge-rewrite tests + update the two pinned-default assertions.
- `carta/hook/tests/test_eval_supersession.py` — **create**.

---

## Task 1: `ollama_json` structured-output helper

**Files:**
- Modify: `carta/hook/judge.py`
- Test: `carta/hook/tests/test_judge.py` (create)

**Interfaces:**
- Produces:
  - `ollama_json(ollama_url: str, model: str, system: str, user: str, *, timeout_s: float = 20, schema=None) -> dict | None`
  - `_extract_json(content: str) -> dict | None` (tolerant parser)

- [ ] **Step 1: Write the failing test**

Create `carta/hook/tests/test_judge.py`:

```python
from carta.hook import judge


def test_extract_json_plain_object():
    assert judge._extract_json('{"conflict": true}') == {"conflict": True}


def test_extract_json_wrapped_in_prose():
    assert judge._extract_json('Sure: {"conflict": false} done') == {"conflict": False}


def test_extract_json_nested_balanced():
    assert judge._extract_json('{"a": {"b": 1}} trailing') == {"a": {"b": 1}}


def test_extract_json_returns_none_on_garbage():
    assert judge._extract_json("no json here") is None
    assert judge._extract_json("") is None


def test_ollama_json_returns_dict(monkeypatch):
    class _Resp:
        def json(self):
            return {"message": {"content": '{"conflict": true}'}}
    monkeypatch.setattr("carta.hook.judge.requests.post", lambda *a, **k: _Resp())
    assert judge.ollama_json("http://x", "m", "sys", "usr", timeout_s=1) == {"conflict": True}


def test_ollama_json_none_on_http_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("carta.hook.judge.requests.post", _boom)
    assert judge.ollama_json("http://x", "m", "sys", "usr", timeout_s=1) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_judge.py -v`
Expected: FAIL — `AttributeError: module 'carta.hook.judge' has no attribute '_extract_json'`.

- [ ] **Step 3: Implement the helper**

In `carta/hook/judge.py`, add `import json` at the top (with the existing imports), then append:

```python
def _extract_json(content) -> dict | None:
    """Parse a JSON object from model output, tolerating prose/markdown wrapping.
    Returns a dict, or None if no JSON object can be parsed."""
    if not isinstance(content, str):
        return None
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start = content.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except ValueError:
                        break
        start = content.find("{", start + 1)
    return None


def ollama_json(ollama_url, model, system, user, *, timeout_s: float = 20, schema=None) -> dict | None:
    """Ask Ollama for a JSON object via /api/chat (format-constrained).

    Returns the parsed dict, or None on any network/parse/format error so the
    caller can fail open.
    """
    try:
        resp = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": schema if schema else "json",
                "keep_alive": ollama_keep_alive(),
            },
            timeout=timeout_s,
        )
        return _extract_json(resp.json()["message"]["content"])
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/hook/tests/test_judge.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add carta/hook/judge.py carta/hook/tests/test_judge.py
git commit -m "feat(judge): ollama_json structured-output helper (fail-open)"
```

---

## Task 2: Rewrite `_stale_judge` as the evidence-citation gate

**Files:**
- Modify: `carta/hook/stale_scan.py` (the `from carta.hook.judge import ...` line near the top; the `_stale_judge` function ~lines 62-82)
- Test: `carta/hook/tests/test_stale_scan.py`

**Interfaces:**
- Consumes: `ollama_json` (Task 1).
- Produces: `_stale_judge(section_text: str, candidate: dict, cfg: dict) -> bool | None` — same signature; now returns `True` only on a cited genuine conflict.

- [ ] **Step 1: Write the failing test**

Append to `carta/hook/tests/test_stale_scan.py`:

```python
def test_stale_judge_true_only_on_conflict(monkeypatch):
    from carta.hook import stale_scan

    cfg = {"embed": {"ollama_url": "http://x"}, "hooks": {"stale_scan": {}}}
    cand = {"source": "docs/a.md", "excerpt": "X was removed and replaced by Y."}

    monkeypatch.setattr(stale_scan, "ollama_json",
                        lambda *a, **k: {"section_claim": "uses X", "doc_clause": "X removed", "conflict": True})
    assert stale_scan._stale_judge("we use X", cand, cfg) is True

    monkeypatch.setattr(stale_scan, "ollama_json",
                        lambda *a, **k: {"section_claim": "uses X", "doc_clause": "X discussed", "conflict": False})
    assert stale_scan._stale_judge("we use X", cand, cfg) is False


def test_stale_judge_none_on_bad_or_missing_output(monkeypatch):
    from carta.hook import stale_scan

    cfg = {"embed": {"ollama_url": "http://x"}, "hooks": {"stale_scan": {}}}
    cand = {"source": "docs/a.md", "excerpt": "..."}

    monkeypatch.setattr(stale_scan, "ollama_json", lambda *a, **k: None)
    assert stale_scan._stale_judge("s", cand, cfg) is None

    monkeypatch.setattr(stale_scan, "ollama_json", lambda *a, **k: {"section_claim": "x"})  # no 'conflict'
    assert stale_scan._stale_judge("s", cand, cfg) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -k stale_judge -v`
Expected: FAIL — `AttributeError: ... has no attribute 'ollama_json'` (the name isn't imported into `stale_scan` yet).

- [ ] **Step 3: Rewrite the judge**

In `carta/hook/stale_scan.py`, update the judge import to add `ollama_json`:

```python
from carta.hook.judge import ollama_json
```
(Replace the existing `from carta.hook.judge import ollama_yesno` line — `ollama_yesno` is no longer used in this module.)

Then replace the entire `_stale_judge` function body with:

```python
def _stale_judge(section_text: str, candidate: dict, cfg: dict):
    """Evidence-citation supersession gate. Returns True/False/None (None on error
    → caller fails open). True only when the judge cites a clause that genuinely
    conflicts with a claim in the section — not merely related/corroborating text."""
    sc = cfg.get("hooks", {}).get("stale_scan", {})
    ollama_url = cfg["embed"]["ollama_url"]
    model = sc.get("ollama_model", "qwen3.5:9b")
    timeout_s = sc.get("judge_timeout_s", 30)
    system = (
        "You decide whether a documentation section has been SUPERSEDED by a "
        "knowledge-base excerpt. A section is superseded ONLY if the excerpt states "
        "something that makes a specific claim in the section wrong, replaced, or "
        "deprecated. Content that is merely related, complementary, corroborating, or "
        "duplicated is NOT supersession. Respond with a JSON object only."
    )
    user = (
        f"Committed section:\n{section_text[:600]}\n\n"
        f"Knowledge-base excerpt ({candidate.get('source', '')}):\n"
        f"{candidate.get('excerpt', '')[:600]}\n\n"
        'Return a JSON object with exactly these keys: '
        '"section_claim" (an exact quote from the committed section), '
        '"doc_clause" (an exact quote from the excerpt), and '
        '"conflict" (true or false). '
        'Set "conflict" to true ONLY if doc_clause makes section_claim wrong, replaced, '
        'or deprecated. If the excerpt merely repeats, supports, or relates to the '
        'section, set "conflict" to false.'
    )
    result = ollama_json(ollama_url, model, system, user, timeout_s=timeout_s)
    if not isinstance(result, dict) or "conflict" not in result:
        return None
    return bool(result["conflict"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -v`
Expected: PASS (new judge tests + all existing stale_scan tests — the `judge_fn` contract is unchanged).

- [ ] **Step 5: Commit**

```bash
git add carta/hook/stale_scan.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(stale-scan): evidence-citation supersession judge (#84)"
```

---

## Task 3: Config defaults — stronger judge model + longer timeout

**Files:**
- Modify: `carta/config.py` (the `hooks.stale_scan` dict)
- Test: `carta/hook/tests/test_stale_scan.py` (lines ~14-15, the pinned-default assertions)

**Interfaces:** none (data defaults). The recall hook's `proactive_recall`/`cross_project_recall` models are NOT touched.

- [ ] **Step 1: Update the failing assertions first**

In `carta/hook/tests/test_stale_scan.py`, change the two default assertions (currently `== 5` and `== "qwen3.5:0.8b"`) to:

```python
    assert sc["judge_timeout_s"] == 30
    assert sc["ollama_model"] == "qwen3.5:9b"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py -k default -v` (or the test that asserts these — find it with `grep -n "judge_timeout_s" carta/hook/tests/test_stale_scan.py`)
Expected: FAIL — `assert 5 == 30`.

- [ ] **Step 3: Change the config defaults**

In `carta/config.py`, in the `"hooks": {"stale_scan": {...}}` block, change exactly these two lines:

```python
            "judge_timeout_s": 30,
            "ollama_model": "qwen3.5:9b",
```
(Leave `enabled`, `block_on_stale`, `candidate_threshold`, `max_judge_calls`, `claude_md_nudge` unchanged. Do NOT change any model under `proactive_recall` or `cross_project_recall`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest carta/hook/tests/test_stale_scan.py carta/tests/test_config.py -v`
Expected: PASS (the recall-hook `0.8b` assertions in test_config.py are unaffected).

- [ ] **Step 5: Commit**

```bash
git add carta/config.py carta/hook/tests/test_stale_scan.py
git commit -m "feat(config): stale_scan judge -> qwen3.5:9b, timeout 30s"
```

---

## Task 4: Eval runner

**Files:**
- Create: `carta/hook/eval/eval_supersession.py`
- Test: `carta/hook/tests/test_eval_supersession.py` (create)

**Interfaces:**
- Consumes: `_stale_judge` (Task 2); the committed `supersession_cases.yaml`.
- Produces: `evaluate(cases: list[dict], judge_fn) -> dict` with keys `tp, fp, tn, fn, errors, precision, recall, rows`; and `main()` (live runner, exits non-zero unless all cases are correct).

- [ ] **Step 1: Write the failing test**

Create `carta/hook/tests/test_eval_supersession.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest carta/hook/tests/test_eval_supersession.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'carta.hook.eval.eval_supersession'`.

- [ ] **Step 3: Write the runner**

Create `carta/hook/eval/eval_supersession.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest carta/hook/tests/test_eval_supersession.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add carta/hook/eval/eval_supersession.py carta/hook/tests/test_eval_supersession.py
git commit -m "feat(eval): supersession precision/recall runner"
```

---

## Task 5: Live eval validation + tune (the gate)

**Files:** none by default (tuning may revisit Task 2's prompt or Task 3's config).

**Interfaces:** none. This task validates the whole change against the committed corpus on live Ollama.

- [ ] **Step 1: Ensure the judge model is available**

Run: `ollama list | grep -i "qwen3.5:9b" || ollama pull qwen3.5:9b`
Expected: `qwen3.5:9b` present.

- [ ] **Step 2: Run the live eval from a Carta repo**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m carta.hook.eval.eval_supersession`
Expected: prints per-case rows + a summary. **Target: `RESULT: PASS — all cases correct`** (FP=0, FN=0, errors=0), i.e. the 9 false-positives are rejected and both true-positives are kept.

- [ ] **Step 3: Interpret the result and act**

- **PASS** → record the printed `s/case` latency. If the slowest case approached the 30s timeout, raise `judge_timeout_s` in `carta/config.py` accordingly and re-run; otherwise leave it. Done.
- **`errors` > 0** (timeouts) → raise `hooks.stale_scan.judge_timeout_s` (e.g. 30 → 60) in `carta/config.py` and re-run. The eval prints latency to guide the value.
- **FP > 0** (a false-positive still affirmed) → the judge prompt is too permissive. Tighten the `_stale_judge` prompt (Task 2) — e.g. add the specific failure mode ("a doc that repeats or is the source of the section's text is NOT a supersession") — and re-run. Keep edits to the prompt only.
- **FN > 0** (a true-positive rejected) → the prompt is too strict, OR `qwen3.5:9b` is not capable enough. First tighten the prompt to clarify what counts as a contradiction; if FN persists across two prompt revisions, escalate the model: set `hooks.stale_scan.ollama_model` to `qwen3.5:27b` in `carta/config.py`, `ollama pull qwen3.5:27b`, and re-run.
- **Cannot reach PASS** after prompt tweaks + the `27b` escalation → stop and report the failing cases and what was tried; this is a design-level escalation for the human (the eval corpus is the evidence).

- [ ] **Step 4: Commit any tuning changes**

If Step 3 changed the prompt or config:

```bash
git add carta/hook/stale_scan.py carta/config.py
git commit -m "tune(stale-scan): judge prompt/model to pass supersession eval"
```

- [ ] **Step 5: Re-run the unit suite after any tuning**

Run: `python -m pytest carta/hook/tests/ carta/tests/test_config.py -q`
Expected: green (if `judge_timeout_s`/`ollama_model` changed, the Task 3 assertions must still match — update them if you changed the values).

---

## Final verification

- [ ] **Full unit suite (no regressions)**

Run: `python -m pytest -q`
Expected: all green vs. the pre-task baseline (1101 passed + the new judge/eval tests).

- [ ] **Live eval passes**

Run: `cd /Users/ian/dev/doc-audit-cc && python -m carta.hook.eval.eval_supersession`
Expected: `RESULT: PASS — all cases correct`.

- [ ] **(Optional) confirm the live pipeline on a real project**

Run: `cd /Users/ian/School/Elementrailer/ET-embed && PYTHONPATH=/Users/ian/dev/doc-audit-cc python -m carta claude-md check`
Expected: far fewer findings than the earlier sweep; spot-check that any remaining finding is a genuine contradiction (and watch the stderr `judge_errors` warning for timeouts).

---

## Spec coverage map

| Spec section | Task(s) |
|---|---|
| Eval harness (fixture + runner) | fixture committed (`d94c067`); runner = Task 4 |
| Structured judge helper (`ollama_json`) | Task 1 |
| `_stale_judge` evidence-citation rewrite | Task 2 |
| Config & model (`qwen3.5:9b`, timeout) | Task 3 |
| Validation loop (run, iterate, set timeout) | Task 5 |
| Unit (mocked) + eval (live) testing | Tasks 1–4 (unit), Task 5 (eval) |
| Fail-open preserved (`None` → judge_errors) | Tasks 1, 2 |
| Non-goals: structural guards / clause surfacing / retrieval recall | not implemented (deferred per spec) |
| `bool \| None` `judge_fn` contract unchanged | Task 2 |
| Recall-hook small model untouched | Task 3 (only `stale_scan` changed) |
