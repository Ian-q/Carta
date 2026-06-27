# Supersession precision fix — evidence-citation judge

**Status:** approved design (brainstorming complete)
**Date:** 2026-06-27
**Branch:** feat/claude-md-sync (next phase of the claude-md-sync line; ships together)
**Tracks:** issue #84 (precision); shared machinery also improves the stale-reference hook (#10)

## Problem

`carta claude-md check` (and the existing stale-reference hook — same `run_stale_scan` /
`_stale_judge` machinery) has ~0% real-world precision. Validated on three real projects with
an adequate judge timeout, it produced 9 findings; adversarial evaluation ruled **all 9 false
positives, high confidence**. Worse, it *missed* genuine supersessions: ET-embed's audit
explicitly says CLAUDE.md's FSM and `g_params_queue` claims are wrong, but the detector flagged
the *corroborating* "Project Overview" section instead.

Root cause is the find-by-similarity → yes/no-judge design: retrieval surfaces *topically
related* docs (not *contradicting* ones), and the tiny 0.8B judge affirms "superseded" without
being required to point at an actual contradiction. RRF rank-normalized scores make a bare
`candidate_threshold` useless (the worst offenders scored a perfect 1.0).

## Goal

Replace the yes/no judge with an **evidence-citation gate**: a capable judge must quote the
contradicting clause from the doc *and* the stale claim from the CLAUDE.md section, and return
"superseded" **only** when the two genuinely conflict — not when they merely relate, corroborate,
or duplicate.

**Success criterion:** on a labeled eval set of 11 cases (9 known FPs + 2 known TPs), the judge
rejects all 9 false-positives and keeps both true-positives.

## Scope boundary (explicit)

This fixes the **judge**: precision (reject non-contradictions) and correct judging *when a good
candidate is retrieved*. It does **not** fix **retrieval recall** — whether search surfaces the
contradicting clause in the first place. In the live run, retrieval surfaced the corroborating
doc for the FSM section, not the audit's contradiction clause; a perfect judge still can't catch
a TP it is never handed. Retrieval-recall (how we *search* for contradictions) is a **separate,
later** concern. The eval set isolates the judge by feeding it the candidate directly.

## Non-goals (deferred — "improve later")

- **Structural guards** (fan-out reject when one doc hits ≥3 sections; near-duplicate reject;
  recency/direction). The evidence-citation gate should subsume these on the eval set; build a
  specific guard only if the eval shows the judge missing a specific FP.
- **Surfacing the cited clause to the agent** (the judge's `doc_clause`/`section_claim` quotes
  would make the agent's drafting easier). Easy win, but a fast-follow — keep v1 the gate only.
- **Retrieval recall** (above).

## Architecture

Two new units plus a focused rewrite of one function. Built in this order so we design against
labels, not intuition.

### Component 1 — eval harness (build first)

**Fixture:** `carta/hook/eval/supersession_cases.yaml` — 11 self-contained labeled cases:

```yaml
- id: fp-pypi-runtime
  label: false_positive
  source: docs/quirks/2026-06-11-pypi-index-lag-after-release-tagging.md
  section_text: |
    ## Runtime
    - Python 3.10 or later ...
  candidate_excerpt: |
    Right after a release is tagged, PyPI's simple index can lag ...
  note: keyword overlap on pip/pipx; no contradicting clause
- id: tp-etembed-fsm
  label: true_positive
  source: docs/audits/2026-06-09-comprehensive-system-audit.md
  section_text: |
    ### State Machine (FSM) — two-layer model
    ... enters ACTIVE on bridge + CAN handshake ...
  candidate_excerpt: |
    The top-level FSM enters ACTIVE on the CAN handshake alone
    (CLAUDE.md's "bridge + CAN handshake" is wrong) ...
  note: explicit clause-level contradiction
  # ... 9 FP + 2 TP total
```

The 9 FP cases are extracted from the re-run results (`section_text` + `candidate_excerpt` as the
detector actually fed the judge). The 2 TP cases are built from ET-embed's CLAUDE.md sections
(FSM, Serial-Bridge) paired with the audit's contradicting clauses. Seeded with 11; designed to
grow, like the ET-embed retrieval corpus.

**Runner:** `carta/hook/eval/eval_supersession.py` — loads the fixture, calls the real judge
(`_stale_judge`) per case with the configured model, compares `conflict` to `label`, and prints
a confusion table + precision/recall. Calls live Ollama, so it is a manual/eval gate, not a CI
unit test (mirrors how `carta eval` validates retrieval). Exit non-zero if any case is wrong, so
it can gate the change.

### Component 2 — structured judge helper (`carta/hook/judge.py`)

Add alongside `ollama_yesno`:

```python
def ollama_json(ollama_url, model, system, user, *, timeout_s=20, schema=None) -> dict | None:
    """Ask Ollama for a JSON object (uses Ollama's format="json" / schema constraint).
    Returns the parsed dict, or None on any network/parse/format error (fail-open)."""
```

It posts to `/api/chat` with `format="json"` (or `format=schema` when provided — Ollama
structured outputs), parses `message.content` as JSON, and returns the dict or `None`. Robust to
prose/markdown wrapping by extracting the first balanced `{...}` if a bare `json.loads` fails.
`None` on any failure (so the caller fails open, and the existing `judge_errors` counter records
it).

### Component 3 — `_stale_judge` rewrite (`carta/hook/stale_scan.py`)

Keep the signature and the `bool | None` return (so `run_stale_scan` and its injectable `judge_fn`
contract are unchanged). Internally:

- New **system prompt**: "You decide whether a documentation section has been SUPERSEDED by a
  knowledge-base excerpt. A section is superseded ONLY if the excerpt states something that makes
  a specific claim in the section wrong, replaced, or deprecated. Merely related, complementary,
  corroborating, or duplicated content is NOT supersession. Return JSON only."
- New **user prompt**: the committed section + the KB excerpt, asking for
  `{"section_claim": "<quote>", "doc_clause": "<quote>", "conflict": true|false}` where
  `conflict` is true only if `doc_clause` makes `section_claim` wrong/outdated.
- Call `ollama_json(...)`; return `bool(result["conflict"])`, or `None` if the result is missing
  / malformed / `conflict` absent (fail-open).

### Component 4 — config & model

`carta/config.py` `hooks.stale_scan`:
- `ollama_model`: `qwen3.5:0.8b` → **`qwen3.5:9b`** (capable enough for clause-level contradiction
  reasoning; the path is not latency-critical).
- `judge_timeout_s`: raise from 5 to a value justified by the eval's measured 9B latency (the
  runner reports per-call latency; pin the default from that, not a guess).

Shared change — the existing pre-push hook also gets the stronger judge. Intended. Latency
tradeoff is bounded by `max_judge_calls`, the path is warn-only/non-blocking, and the model is
config-tunable. The `judge_errors` visibility (already shipped) surfaces any timeouts.

## Data flow (unchanged except inside the judge)

`run_stale_scan` → per candidate above threshold → `_stale_judge(section, candidate, cfg)` →
**[NEW]** evidence-citation prompt → `ollama_json` (qwen3.5:9b, format=json) → parse `conflict`
→ `bool | None` → finding emitted only when `conflict is True`.

## Error handling — fail-open preserved

`ollama_json` returns `None` on network error, timeout, invalid JSON, or missing `conflict`;
`_stale_judge` propagates `None`; `run_stale_scan` treats `None` as "not flagged" and increments
`judge_errors` (visible per the shipped fix). No new crash path; no false "stale" on judge
failure.

## Testing

- **Unit (mocked, CI — no Ollama):**
  - `ollama_json` returns the parsed dict on well-formed JSON; `None` on malformed/empty/HTTP
    error; extracts JSON wrapped in prose.
  - `_stale_judge` with a patched `ollama_json`: `{"conflict": true}` → `True`;
    `{"conflict": false}` → `False`; missing key / `None` → `None` (fail-open).
  - `run_stale_scan` unchanged-contract regression (existing tests stay green).
- **Eval (live Ollama, manual gate):** `eval_supersession.py` over the 11 cases — must reject
  all 9 FPs and keep both TPs (precision 100% / recall 100% on the set). If the 9B model fails
  cases, iterate the prompt; if it still fails, escalate the model (e.g., `qwen3.5:27b`) — the
  fixture makes this measurable.

## Validation loop (how we know it works)

1. Build fixture + runner.
2. Rewrite the judge; run the eval.
3. All 11 correct → done. Otherwise iterate prompt, then model, re-running the eval until the
   success criterion holds. Record the final model + measured latency → set `judge_timeout_s`.

## Follow-ups (filed / to file)

- Retrieval recall for contradictions (separate spec) — the likely next area.
- Structural guards + cited-clause surfacing (deferred non-goals above).
- Re-run the full real-project sweep after the fix to confirm live precision matches the eval.
