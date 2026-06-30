# Field notes — operating Carta (for agents)

Real lessons from dogfooding Carta on live projects (ET-embed, petsense). Read this before
running `carta claude-md`, the stale hook, or a full embed against an unfamiliar setup. Each
item is something that actually bit a session, with the fix.

## Running an unreleased / branch build against another repo

To exercise branch code (not the installed `carta`) on a *different* project, run from that
project's directory with the checkout on `PYTHONPATH`:

```bash
cd /path/to/target-repo
PYTHONPATH=/path/to/carta-checkout python -m carta <command>
```

- Use the interpreter that has Carta's deps (`qdrant_client`, `requests`, `yaml`, …). A bare
  `python3` often resolves to a different interpreter that lacks them — symptom: `run_search`
  returns 0 hits or `ModuleNotFoundError: No module named 'carta'`. Check with
  `python -c "import carta, qdrant_client, yaml"`.
- From inside the target repo (no local `carta/` package), `PYTHONPATH` wins the import. From
  inside the Carta repo itself, the local package shadows `PYTHONPATH` — verify which you got.

## The supersession judge (claude-md sync + stale hook): latency & `num_ctx`

`carta claude-md check` and the stale-reference hook call an Ollama judge (`hooks.stale_scan.
ollama_model`, default `qwen3.5:9b`) once per candidate. Two things to know:

- **Latency is dominated by the judge's loaded context window, not model size.** If Ollama
  loads the model with a large `num_ctx` (e.g. 65536), a single yes/no judge call can take
  60–200s+ even for a small model. Check it:

  ```bash
  ollama ps   # look at the CONTEXT column for the judge model
  ```

  If it's huge, that's your latency. Levers: a smaller judge `num_ctx`, GPU headroom (don't
  keep large models resident alongside), or a faster judge model. A degraded daemon (thrashing
  after sustained load) also slows down over time — `ollama stop <model>` / restarting the
  daemon recovers it.

- **A `0 findings` result with `judge_errors > 0` is NOT "in sync".** A judge call that times
  out returns no verdict (fail-open), which the tooling now reports as `judge_errors` with a
  loud stderr warning. Treat it as *incomplete*, raise `hooks.stale_scan.judge_timeout_s`
  (the judge runs at pre-push / end-of-session — latency-tolerant), and re-run. The default is
  60s; on a slow host you may need 120–200s.

## Validating judge / retrieval changes: the eval corpus

Don't eyeball precision. There's a labeled corpus + runner:

```bash
python -m carta.hook.eval.eval_supersession   # precision / recall over carta/hook/eval/supersession_cases.yaml
```

`PASS` = false-positives rejected *and* true-positives caught (FP=0, FN=0, errors=0). When you
find a real false-positive (or a missed supersession) in the wild, add it to
`supersession_cases.yaml` with its label and re-run — that's how the judge stays honest. (A
real recall regression was caught this way: a 600→1500 char widening of the judge window,
because the contradicted claim sat *after* the matching prose and was being truncated off.)

## The `/claude-md-sync` end-of-session flow

1. `carta claude-md check` → JSON findings (each: heading, section text, the superseding doc +
   excerpt). **Heed `judge_errors`** (above) before trusting `0 findings`.
2. Draft a correction per genuinely-superseded section — using the cited clauses *and* your
   session context. Only correct *descriptive* claims; never "correct" a durable directive
   toward what the code happens to do.
3. Show diffs, apply only what the human approves. Carta never edits CLAUDE.md itself.
4. `carta claude-md record` to checkpoint the sync sidecar so unchanged sections skip next run.

Expect *few* findings — the evidence-citation judge is precise (on petsense it went from 3
false-positives under the old yes/no judge to 1 genuine finding).

## Corpus drift: `doctor` → `embed --repair`

`carta doctor`'s "Corpus integrity" section surfaces real drift — count mismatches
(sidecar vs Qdrant), empty chunks (image-only PDFs that extracted no text), orphaned points,
slug collisions. The fix it recommends works:

```bash
carta embed --repair      # re-embeds damaged TEXT points (cleared petsense's mismatches: 20 re-embedded)
carta embed --visual      # needed for the VISUAL count mismatches — requires the torch/ColPali env
```

- `--repair` fixes text-side damage and re-queues visual pages; it does **not** fix slug
  collisions (a naming issue, not damaged points — see Known quirks).
- The visual pass (`--visual` / ColPali) needs `torch`, which lives in the `carta-cc` pipx
  venv, not a bare `python`. A plain `python -m carta embed --visual` silently skips ColPali.

## Known quirks found dogfooding (tracked issues)

- **Judge latency on a 64K-`num_ctx` host** — [#86]. The judge logic is correct; the host's
  Ollama is the bottleneck. `judge_errors` makes it visible, never a silent false "in sync".
- **`carta status` always shows "0 done"** — [#88]. Status counts the phantom `"done"` status,
  but embed writes `"embedded"`. Cosmetic; counts land in "other".
- **Slug collisions** — [#89]. `slug_from_filename` drops the extension and ignores the
  directory, so `foo.md`/`foo.pdf` and `dir1/foo.md`/`dir2/foo.md` collide. Not fixed by
  `--repair`.

[#86]: https://github.com/Ian-q/Carta/issues/86
[#88]: https://github.com/Ian-q/Carta/issues/88
[#89]: https://github.com/Ian-q/Carta/issues/89
