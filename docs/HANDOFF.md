# Handoff primer — 2026-10-17

Current state and the next moves, for a session picking this up cold. Rewritten at each
handoff; it is not a log. Durable *why* lives in [`ROADMAP.md`](ROADMAP.md), operating
lessons in [`field-notes.md`](field-notes.md), per-issue status on
[Project #4](https://github.com/users/Ian-q/projects/4).

Private project names, paths and hosts are written as placeholders (`proj-a`, `~/work/…`,
`qdrant.example.ts.net`, `192.0.2.10`); the real values are in the maintainer's auto-memory.

**`main` = `7037d00` (0.18.0), pushed, suite 2530 passed / 3 skipped.** Working tree carries one
intentional local edit (`.carta/config.yaml` — this machine's model/URL choices) plus two
`.bak` files; leave them alone.

## What just happened (2026-10-17)

- **History rewrite.** A judge eval fixture (`carta/hook/eval/supersession_cases.yaml`) quoted a
  private project's docs verbatim. It was purged from every commit with `git filter-repo`, names,
  paths and hosts were replaced throughout (commit messages too), and all 56 tags were
  rewritten. The fixture now holds fictional cases of the same shape. **Every SHA changed:**
  never push a branch or clone made before this; re-fetch and rebuild work on the new `main`.
  Local pre-rewrite branches were renamed `pre-rewrite/*`; a backup of the old history lives
  outside the repo (see auto-memory). GitHub may still serve old commits by SHA until a
  support purge.
- **Tidy-up (#135):** MIT `LICENSE`, PyPI metadata (description, licence, URLs), untracked
  scratch, `BUGS.md` removed, images moved to `docs/assets/`, issue template, merged remote
  branches deleted and auto-delete-on-merge turned on.
- **0.18.0 released:** the graph arm (#129–#131), HyDE (#126), cross-encoder judge (#127),
  calibrated gate (#128), the judge-thinking fix (#125), eval metrics (#124). PyPI, GitHub
  Release, version-bump commit and the local pipx install are all at 0.18.0.

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

## Release mechanics

`release.yml` fires only on a `v*` tag and then: parses the version from the tag → rewrites
`carta/__init__.py`, `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` →
commits and pushes the bump to `main` → builds → publishes to PyPI → creates a GitHub Release
with generated notes. **It does not touch `CHANGELOG.md`.** So:

1. Move the `## [Unreleased]` entries under `## [X.Y.0] — <date>` with a lead line, commit on `main`.
2. `git tag vX.Y.0 && git push origin vX.Y.0`, then watch the Release workflow.
3. Verify the endpoints, not the badge: `https://pypi.org/pypi/carta-cc/X.Y.0/json` (the bare
   `/pypi/carta-cc/json` lags for minutes — see `docs/quirks/`), the GitHub Release, the bump
   commit on `main`. Locally, `pipx upgrade` sees the lag too: `pipx runpip carta-cc install
   --no-deps carta-cc==X.Y.0` installs past it without touching torch/fastembed.
4. Re-pull: the workflow pushed a commit, so local `main` is behind.

## Open threads

- **#118 (reopened)** — it asks for a `SessionEnd`/`Stop` hook that reads the transcript
  plus that session's trace lines and labels each injection used / ignored / unhelpful.
  #128 only removed its stated complaint (guessed constants); the feedback loop does not
  exist. Trace records already carry `judge_backend` and `judge_score` for those labels to
  attach to. This is the only source of labels from *real behaviour* — an eval corpus can
  say whether a doc answers a question, never whether an injection helped.
- **The v2 eval set is uncommitted and exists on one disk**, at
  `~/work/proj-a/.carta/eval/proj-a-v2.yaml` (untracked there). 84
  queries, agent-built and machine-checked but **not hand-verified**; each carries an
  `evidence` quote for review. It belongs in proj-a's git, **not** this public repo —
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

- **Qdrant is remote** at `http://qdrant.example.ts.net:6333` (homelab, tailnet).
  Ollama: this Mac holds only `nomic-embed-text` (+ `qwen3.5:2b`) by design — query
  embedding stays local while ingest/OCR wants the homelab GPU at `192.0.2.10:11434`,
  and one `ollama_url` cannot serve both (#116). The stale-scan 9b judge is absent here.
- **fastembed is an optional `[hybrid]` extra**, and CI installs the bare package. Anything
  defaulting to it must degrade gracefully — that is why `judge_backend` is `auto`. Run the
  suite **both ways**: a pytest plugin that makes `find_spec("fastembed")` return None *and*
  blocks the import (a finder merely returning None is a no-op and proves nothing).
- **Tests must never touch a real model.** Three gray-zone tests once passed only because
  this machine had the cross-encoder cached, so the real model scored their dummy text; CI
  caught it. Patch `carta.search.rerank.score_pairs`.
- **Branch code against another project**: run from that project with
  `PYTHONPATH=~/dev/carta ~/.local/pipx/venvs/carta-cc/bin/python -m carta …`
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
Project #4 with `Area` and `Size`. No proj-a/ProjectA content in this public repo —
aggregate numbers only.
