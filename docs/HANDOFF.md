# Handoff primer — 2026-09-29

Current state and the next moves, for a session picking this up cold. Rewritten at each
handoff; it is not a log. Durable *why* lives in [`ROADMAP.md`](ROADMAP.md), operating
lessons in [`field-notes.md`](field-notes.md), per-issue status on
[Project #4](https://github.com/users/Ian-q/projects/4).

**`main` = `8eb2b38`, pushed, suite 1479 passed / 4 skipped.** Working tree carries one
intentional local edit (`.carta/config.yaml` — this machine's model/URL choices) plus two
`.bak` files; leave them alone.

## What just shipped

| PR | Change |
|---|---|
| #124 | `carta eval`: recall@1/@3, `reject:` hard negatives, per-tag breakdown, `--json` |
| #125 | Recall judge sends `think: false` — it could never answer inside its 3s budget |
| #126 | **HyDE**: optional `hypothetical` on MCP `carta_search`, `carta search --hypothetical`, `/doc-search` writes one |
| #127 | **Cross-encoder gray-zone judge** (`judge_backend: auto`) + `carta/hook/eval/calibrate.py`; fastembed cache moved to `~/.carta/models/fastembed` |
| #128 | **Calibrated gate**: `low_threshold` 0.65, `agree_rank` 1 |
| `8eb2b38` | `carta doctor` checks `glm-ocr:latest` is pulled |

Pushing the long-unpushed 0.17.0 commits auto-closed **#19, #120, #121, #122**.

## Decisions already settled — do not re-derive these

- **HyDE belongs to the caller.** A frontier caller's hypothetical answer lifts MCP search
  MRR 0.463 → 0.698 (recall@5 0.54 → 0.77); a local `qwen3.5:9b` writer does **not** help
  (CI crosses zero). Carta never generates one. Blend is `mean`, not hypothetical-only.
- **A deliberately wrong answer is not a usable anchor.** Embeddings encode topic, not
  truth. Subtracting one is neutral-to-harmful; its "opposite" vector found 0 of 84 golds.
- **The recall judge is a cross-encoder, not an LLM.** AUC 0.915 vs unrelated chunks at
  ~10 ms warm / 0.46 s cold in a fresh process — and the hook *is* a fresh process per
  prompt, which is why Laya (53 ms warm but ~2 s cold + torch) lost despite similar AUC.
- **Score LLM judges with logprobs, never a bare yes/no token.** `qwen3.5:2b` went 0.78 →
  0.966 AUC on identical data from that change alone. A binary-vs-continuous comparison is
  rigged; this mistake was made once already.
- **The dense cosine does not separate relevant from irrelevant top hits** (71% of
  unrelated ones outscore the weakest relevant one). It only separates "nothing answers
  this" (0.600–0.659) from everything else — hence `low_threshold: 0.65` as a floor, with
  the judge doing the discriminating.
- **Reasoning models must be told not to think on budgeted surfaces**, and with a `format`
  constraint Ollama does **not** count thinking tokens in `eval_count`, so it hides as
  unexplained latency. The stale-scan judge deliberately *keeps* thinking (precision, and
  it runs pre-push); a test pins that.

## Next up: cut a release (nothing is published yet)

`carta/__init__.py`, `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` all
say **0.17.0**, but **no `v0.17.0` tag exists anywhere** — so 0.17.0 was prepared and never
released, and everything above sits unreleased on top of it. **PyPI's latest is 0.16.1**, which
is also what `pipx` has installed here.

`release.yml` fires only on a `v*` tag and then: parses the version from the tag → rewrites
those three version files → commits and pushes the bump → builds → publishes to PyPI →
creates a GitHub Release with generated notes. **It does not touch `CHANGELOG.md`.**

So the clean path is **0.18.0**, not a late `v0.17.0` (tagging that would relabel five PRs
of work as 0.17.0):

1. Move the `## [Unreleased]` entries (Changed / Added / Fixed — currently the five PRs
   above) under `## [0.18.0] — <date>`, leaving `[Unreleased]` empty. Give it a lead line
   in the style of the 0.17.0 section.
2. Commit the CHANGELOG on `main`.
3. `git tag v0.18.0 && git push origin v0.18.0`, then watch the Release workflow.
4. Verify all four endpoints afterwards: PyPI, GitHub Release, the version-bump commit the
   workflow pushes to `main`, and `pipx upgrade carta-cc`. Past releases logged a benign
   git exit-128 annotation in that workflow — check the endpoints, not just the badge.
5. Then re-pull: the workflow pushes a commit, so local `main` will be behind.

## Open threads

- **#118 (reopened)** — it asks for a `SessionEnd`/`Stop` hook that reads the transcript
  plus that session's trace lines and labels each injection used / ignored / unhelpful.
  #128 only removed its stated complaint (guessed constants); the feedback loop does not
  exist. Trace records already carry `judge_backend` and `judge_score` for those labels to
  attach to. This is the only source of labels from *real behaviour* — an eval corpus can
  say whether a doc answers a question, never whether an injection helped.
- **The v2 eval set is uncommitted and exists on one disk**, at
  `~/School/Elementrailer/ET-embed/.carta/eval/et-embed-v2.yaml` (untracked there). 84
  queries, agent-built and machine-checked but **not hand-verified**; each carries an
  `evidence` quote for review. It belongs in ET-embed's git, **not** this public repo —
  it is company content. The predecessor was lost from disk exactly this way.
- **Watch the judge zone in practice.** It went from always-silent (the thinking bug) to
  actively judging. If it turns noisy, the measured alternatives are `qwen3.5:2b` +
  logprobs (AUC 0.940, 237 ms) or a resident-process judge; both are characterised.
- **Laya** is not dead, just aimed wrong: its edge is one typed call answering several
  questions at once (relevant? superseded? needs deep scan?). Revisit only if such a
  multi-decision surface appears. Its loader wastes ~31 s randomly initialising weights it
  immediately overwrites — wrap in `transformers.initialization.no_init_weights()`.
- `qwen3.5:2b` (2.7 GB) sits on this Mac and is only used if `judge_backend: ollama`.

## Environment and gotchas that cost time

- **Qdrant is remote** at `http://qdrant.tail6f0591.ts.net:6333` (homelab, tailnet).
  Ollama: this Mac holds only `nomic-embed-text` (+ `qwen3.5:2b`) by design — query
  embedding stays local while ingest/OCR wants the homelab GPU at `100.109.75.49:11434`,
  and one `ollama_url` cannot serve both (#116). The stale-scan 9b judge is absent here.
- **fastembed is an optional `[hybrid]` extra**, and CI installs the bare package. Anything
  defaulting to it must degrade gracefully — that is why `judge_backend` is `auto`. Run the
  suite **both ways**: a pytest plugin that makes `find_spec("fastembed")` return None *and*
  blocks the import (a finder merely returning None is a no-op and proves nothing).
- **Tests must never touch a real model.** Three gray-zone tests once passed only because
  this machine had the cross-encoder cached, so the real model scored their dummy text; CI
  caught it. Patch `carta.search.rerank.score_pairs`.
- **Branch code against another project**: run from that project with
  `PYTHONPATH=/Users/ian/dev/doc-audit-cc ~/.local/pipx/venvs/carta-cc/bin/python -m carta …`
  (that venv holds torch/fastembed; the default python does not).
- **Recalibration** (after changing `judge_model`, embedding model, or corpus):
  ```bash
  python -m carta.hook.eval.calibrate chunks <qdrant_url> <collection> chunks.jsonl
  python -m carta.hook.eval.calibrate judge  <eval.yaml> chunks.jsonl     # judge_threshold
  python -m carta.hook.eval.calibrate gate   <eval.yaml>                  # low_threshold, agree_rank
  ```
  `judge_threshold` is a **raw model-specific logit, not a probability**.
- `/private/tmp` scratchpads are reaped after a few days. Durable working data goes in
  `~/.carta/` (see `~/.carta/calib/`) or a repo.

## Conventions to keep

Superpowers flow for anything non-trivial (brainstorm → spec → plan → TDD → verify).
Isolate multi-step work in a `git worktree` under `.claude/worktrees/` — this checkout gets
concurrent branch switching. PRs squash-merge into `main`; **always write `Closes #N`** in
the body (and do not over-claim it — #118 was closed wrongly once). New issues go on
Project #4 with `Area` and `Size`. No ET-embed/Elementrailer content in this public repo —
aggregate numbers only.
