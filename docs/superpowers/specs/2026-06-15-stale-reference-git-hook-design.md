---
id: 2026-06-15-stale-reference-git-hook-design
title: Stale-reference git hook (pre-push / pre-commit)
status: draft
related: [2026-06-14-contextual-chunk-headers-design]
date: 2026-06-15
related_issue: Ian-q/Carta#10
---

# Stale-reference git hook (#10, slice 1 of 4)

## Background

Carta can already surface stale references via `/doc-search`, but only when a
human asks. Issue #10 proposes making stale-reference detection **ambient** via
four mechanisms: (1) a local git hook, (2) a CI/PR diff scan, (3) query-time
context hints, and (4) expanded CLAUDE.md guidance. Those are four distinct
subsystems; this spec covers **only slice 1, the local git hook** — the
highest-value, most self-contained piece. The other three get their own
spec → plan → implementation cycles and reuse the scan core built here.

### Motivating example

After migrating from micro-ROS to a COBS+JSON serial bridge, three docs were
left with stale content (`teensy-io-allocation.md` still had a "micro-ROS UART"
section, etc.). **None were annotated** as deprecated — the staleness was only
discoverable by comparing each stale doc against the authoritative replacement
(`cobs-json-serial-bridge-design.md`), which was already embedded in the graph.
No human caught it until much later. The hook should catch this class of drift
before it leaves the developer's machine.

## Decisions (from brainstorming)

- **Detection = semantic + LLM judge.** The graph has no built-in
  deprecated/superseded signal, and the motivating case had no annotations, so
  metadata-driven detection would have missed it. Instead, mirror the existing
  proactive-recall hook: search the graph for the changed content's topic, then
  let the small Ollama judge decide whether a strong external match indicates the
  changed content has been **superseded**. Zero new metadata; no embed-pipeline
  change.
- **Scope = Carta-tracked docs only.** Only changed files inside Carta's doc scope
  are scanned. The graph is doc-centric; doc-vs-doc supersession is what the judge
  can assess well. Code/config scanning is deferred (it belongs to the CI diff
  scan slice and would be much noisier).
- **Stage = pre-push by default, stage-selectable.** A semantic + LLM-judge scan
  is too heavy to run on every `git commit`. **pre-push** fires once per push,
  amortizes the latency across a batch of commits, still gives local feedback
  before anything reaches the remote, and reviews the whole outgoing changeset.
  The scan core is stage-agnostic, so `--stage pre-commit` remains a cheap opt-in
  for anyone who wants the earliest gate. PR-level enforcement is **not** this
  slice — that is the CI diff scan (slice 2).
- **Install = managed git hook.** `carta hook install` writes a tiny managed shim
  into `.git/hooks/<stage>` that calls the real runner. Heavy logic lives in
  testable Python.

## Architecture

The design separates **collection** (thin, git/stage-specific) from **scanning**
(the core logic, heavily tested). Collection turns "what changed" into a list of
`ChangedDoc(path, text)`; the scan core consumes that list and is identical for
both stages.

### Change collection (stage-specific)

Both collectors filter to Carta's doc scope by reusing the scanner's
doc-inclusion rules + `is_excluded`, and both return `list[ChangedDoc]`.

- **pre-push (default).** Git passes `<local_ref> <local_sha> <remote_ref>
  <remote_sha>` lines on stdin. For each line the scan range is
  `<remote_sha>..<local_sha>`; when `<remote_sha>` is the zero OID (a new branch
  not yet on the remote), fall back to the commits unique to the push relative to
  the default branch (`$(git merge-base <default_branch> <local_sha>)..<local_sha>`).
  Changed paths come from `git diff --name-only --diff-filter=ACM <range>`;
  content is the **pushed** version via `git show <local_sha>:<path>`. Manual
  invocation with no stdin falls back to `@{upstream}..HEAD` (or the default
  branch when there is no upstream).
- **pre-commit.** Changed paths come from
  `git diff --cached --name-only --diff-filter=ACM`; content is the **staged**
  blob via `git show :<path>` — so we scan exactly what is being committed, not
  the working tree.

### Scan core (stage-agnostic)

For each `ChangedDoc`:

1. **Section it.** Reuse the embedding chunker (`chunk_text`) to split the content
   into heading-anchored sections — the same unit the graph stores. This gives the
   judge a small, focused comparison and lets warnings pinpoint the suspect
   heading.
2. **Per section: search → gate → judge.**
   - Run `run_search(section, search_cfg)` with rerank and ColPali forced **off**
     (the prompt hook's latency pattern).
   - **Exclude the changed file's own hits** (drop results whose `source` equals
     the file's repo-relative path).
   - If the top remaining match scores below `candidate_threshold`, stay silent
     (no plausible superseding doc — the judge is never called).
   - Otherwise run the **stale judge**: a strict yes/no on the small Ollama model.
     The changed section is X; the top external match is Y. Ask whether Y indicates
     X has been **replaced or deprecated** (not merely related or complementary).
     The prompt biases toward "no" to protect precision.
3. **Collect findings.** Each "yes" yields a `StaleFinding` naming the file,
   section heading, the authoritative doc, and the score.

**Departure from the three-zone gate:** there is no "high score → skip judge"
fast-path. High semantic similarity does *not* imply supersession (a doc is most
similar to its own topic), so the score only gates *whether a candidate is worth
judging* — the judge always makes the supersession call.

### Exit code & reporting

Findings go to stderr, naming the file, the suspect section heading, the
authoritative doc + score, and a `/doc-search` hint. Warn-only by default
(exit 0). `block_on_stale: true` → exit 1 when there is at least one finding.

### Fail-open

Mirror the prompt hook: missing Qdrant/Ollama, judge timeout, malformed git
output, or any unexpected exception → exit 0. A push/commit is **never** blocked
by Carta infrastructure; only `block_on_stale: true` combined with a real finding
produces exit 1.

### Components

- **`carta/hook/stale_scan.py`** — the scan core plus collectors.
  - `run_stale_scan(repo_root, cfg, changed_docs, *, search_fn=None, judge_fn=None) -> StaleScanResult`
    — pure core; `search_fn`/`judge_fn` injectable for tests (default to
    `run_search` and the shared judge). Never calls `sys.exit`.
  - `collect_staged(repo_root, cfg) -> list[ChangedDoc]` and
    `collect_pushed(repo_root, cfg, stdin_lines) -> list[ChangedDoc]` — the two
    thin git collectors.
- **`carta/hook/judge.py`** *(small targeted refactor)* — extract the Ollama
  yes/no judge currently private inside `hook.py` into a reusable
  `ollama_yesno(system, user, *, model, timeout_s) -> bool | None` (returns `None`
  on error/timeout). `hook.py`'s `_call_ollama_judge` / `_judge_with_timeout` are
  rewritten to call it, so the prompt hook and stale-scan hook share one judge.
- **`carta/hook/git_hook.py`** — `install_hook(repo_root, stage)` /
  `uninstall_hook(repo_root, stage)` writing/removing the managed shim.
- **CLI** — a `carta hook` subcommand group dispatched by `cmd_hook(args)`.

### Data types

```python
@dataclass
class ChangedDoc:
    path: str            # repo-relative doc path
    text: str            # changed/staged content to scan

@dataclass
class StaleFinding:
    file: str            # repo-relative doc path
    section: str         # heading of the suspect section ("" if none)
    snippet: str         # short excerpt of the changed section
    candidate_path: str  # authoritative doc the graph matched
    candidate_score: float

@dataclass
class StaleScanResult:
    findings: list[StaleFinding]
    scanned: int            # docs scanned
    judge_calls: int        # judge invocations actually made
    skipped_overflow: int   # judge calls skipped after max_judge_calls cap
```

## Install / uninstall

`carta hook install` writes a 2-line managed shim to `.git/hooks/<stage>`
(default `pre-push`), wrapped in sentinels:

```sh
# >>> carta managed >>>
carta hook check --stage pre-push || exit $?
# <<< carta managed <<<
```

(The `pre-commit` shim is identical but omits stdin and passes
`--stage pre-commit`. The `pre-push` shim forwards stdin ref lines to the runner.)

- **No existing hook** → write the shim file and `chmod +x`.
- **Existing Carta-managed hook** → idempotent refresh (no duplicate block).
- **Existing foreign hook** → never clobber. Print the one-line snippet for the
  user to chain it in, and exit non-zero.
- `--uninstall` removes the managed shim file, or just the sentinel block if it
  was chained into a foreign hook.

Nothing auto-installs. `carta init` is unchanged in this slice; installation is an
explicit opt-in.

## CLI surface

| Invocation | Effect |
|------------|--------|
| `carta hook install` | Install the managed hook (default `--stage pre-push`) |
| `carta hook install --stage pre-commit` | Install as a pre-commit hook instead |
| `carta hook install --uninstall` | Remove the managed hook |
| `carta hook check [--stage {pre-push,pre-commit}]` | Run the scan (also what the shim calls); reads ref lines from stdin when `--stage pre-push` |

## Configuration

New `hooks.stale_scan` block in `config.DEFAULTS`, deep-merged like the other
nested sections:

```yaml
hooks:
  stale_scan:
    enabled: true            # runner no-ops (exit 0) if false
    block_on_stale: false    # warn-only default; true → exit 1 on a finding
    candidate_threshold: 0.65 # min external graph-match score to bother judging
    judge_timeout_s: 5
    ollama_model: qwen3.5:0.8b
    max_judge_calls: 30      # hard cap per run; overflow reported, never silent
```

## Output

```
carta stale-scan: scanned 2 doc(s)...
  ⚠  docs/hardware/vcu/teensy-io-allocation.md
     Section "micro-ROS UART" may be stale — knowledge base suggests it was
     replaced (docs/.../cobs-json-serial-bridge-design.md, score 0.91).
     Run: /doc-search "micro-ROS UART"
  (warn-only; set hooks.stale_scan.block_on_stale: true to fail the push)
```

When `max_judge_calls` is hit, the run prints how many sections went unjudged —
the cap is never silent.

## Error handling

- Not a git repo / `git` missing → exit 0 with a one-line stderr note.
- `hooks.stale_scan.enabled: false` → exit 0 immediately, no work.
- No changed docs in scope → exit 0, silent (no output).
- Qdrant/Ollama unreachable, search error, judge timeout/`None` → treated as "no
  finding" for that section; the push/commit proceeds (exit 0).

## Testing (TDD)

**Scan core** (`run_stale_scan` with injected `search_fn`/`judge_fn`, fed a list
of `ChangedDoc`):

- stale section + judge returns `True` → one `StaleFinding` emitted.
- related section + judge returns `False` → no finding.
- top external match below `candidate_threshold` → judge never called, no finding.
- judge returns `None` (timeout/error) → fail-open, no finding.
- the changed file's own hits are filtered out before the candidate gate.
- non-doc files are excluded by the scope filter at collection time.
- `max_judge_calls` cap → judging stops, `skipped_overflow` reflects the remainder.

**Collectors:** `collect_staged` and `collect_pushed` against a temp git repo (or
mocked `subprocess`): the `--diff-filter=ACM` selection, staged-blob vs
pushed-tip content, the zero-OID new-branch range fallback, and the scope filter.

**Install/uninstall** (`git_hook.py`): fresh write to a temp `.git/hooks/pre-push`
(and `pre-commit`); foreign-hook refusal + chain snippet; `--uninstall` removes the
managed block; re-install is idempotent.

**CLI exit codes:** warn-only → exit 0; `block_on_stale: true` + finding → exit 1;
Qdrant/Ollama down → exit 0 (fail-open).

## Out of scope (other #10 slices)

- CI / PR diff scan (`carta scan --diff … --report github-annotations`).
- Query-time context hints on agent message submission.
- Expanded CLAUDE.md `/doc-search` guidance block in bootstrap.
- Scanning changed **code/config** (vs docs).
- Explicit `superseded_by:` / `status: deprecated` frontmatter metadata.

## Acceptance

- `carta hook install` installs a working managed `pre-push` hook (and
  `--stage pre-commit` installs a pre-commit hook); `--uninstall` cleanly removes
  it; foreign hooks are never clobbered.
- On push, a changed doc whose section has been superseded by an authoritative
  doc in the graph produces a warning naming the section and the replacement.
- A merely-related (non-superseding) match produces no warning.
- The hook never blocks on infrastructure failure; it blocks only when
  `block_on_stale: true` and a real finding exists.
- All new behaviour is covered by tests written test-first, and the full suite
  stays green.
