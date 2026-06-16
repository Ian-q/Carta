---
id: 2026-06-16-stale-reference-diff-scan-design
title: Stale-reference diff-range scan (local pre-PR audit)
status: draft
related: [2026-06-15-stale-reference-git-hook-design]
date: 2026-06-16
related_issue: Ian-q/Carta#10
---

# Stale-reference diff-range scan (#10, slice 2 of 4)

## Background

Slice 1 shipped the local stale-reference git hook: `carta hook install` writes a
managed `pre-push` (or `pre-commit`) shim, and `carta hook check` collects the
docs changed by the staged set / pushed commit range, sections them, searches the
knowledge graph per section, and runs a small Ollama judge that warns when a
section appears **superseded** by an authoritative doc in the graph. Everything
fails open. The reusable core is `run_stale_scan` in `carta/hook/stale_scan.py`.

Issue #10 originally framed slice 2 as a **CI / PR diff scan** (`carta scan
--diff … --report github-annotations` emitting GitHub `::warning::` annotations).
This spec deliberately **does not** build that. The scan is *semantic*: it needs
the embedded knowledge graph (Qdrant) and an embedder (Ollama) reachable, plus a
Carta config. A GitHub-hosted CI runner has none of these, so a remote-CI path
would require exporting the embeddings as an artifact, standing up Qdrant + Ollama
as service containers, importing, and scanning against a graph only as fresh as
the last export — heavy infrastructure for a warn-only check.

A developer's machine already runs the live graph. So slice 2 is reframed as a
**local, on-demand pre-PR audit**: scan every doc changed across the whole branch
versus its base, run locally against the live graph, with human-readable output.

### How this differs from the slice-1 pre-push hook

- The **pre-push hook** scans the docs changed in each push's *delta*, automatically, on every push.
- The **diff-range scan** scans every doc changed across the **whole branch vs its base** (`<base>...HEAD`), **on demand**. You run it deliberately to review the entire PR's doc changes at once — before opening the PR, or while it's open to re-check.

## Decision summary

- **Local-first, not remote CI.** No GitHub annotations, no PR-comment posting, no sample CI workflow in this slice.
- **Plain output only.** Reuse the existing `_print_stale_result` human reporter; no machine-readable format, and therefore no line-number / heading-line mapping (line numbers were only needed for annotations).
- **Warn-only by default.** Exit 0 even on findings; `--fail-on-stale` (or `block_on_stale: true` in config) forces exit 1.

## Design

### CLI surface — two new flags on `carta hook check`

```
carta hook check [--stage pre-push|pre-commit] [--diff [RANGE]] [--fail-on-stale]
```

- `--diff [RANGE]` — scan the docs changed across a git range instead of the
  staged set / pushed commits. The argument is optional:
  - `--diff` (no value) → default range `<default-branch>...HEAD`, where the
    default branch is resolved by the existing `_default_branch(repo_root)`
    helper (falls back to `main`).
  - `--diff origin/main...HEAD` (explicit value) → that range verbatim.
  - When `--diff` is absent, `check` behaves exactly as in slice 1 (staged for
    `pre-commit`, pushed-range for `pre-push`). `--diff` and `--stage` are
    independent; `--stage` is ignored when `--diff` is supplied.
- `--fail-on-stale` — exit 1 when any finding exists. Default (flag absent) stays
  exit 0 (warn-only). `block_on_stale: true` in `hooks.stale_scan` config also
  forces exit 1, so the flag is an ad-hoc, per-invocation equivalent.

### New collector — `collect_range`

Add to `carta/hook/stale_scan.py`:

```
collect_range(repo_root: Path, cfg: dict, range_spec: str) -> list[ChangedDoc]
```

- `git diff --name-only --diff-filter=ACM <range_spec>` lists changed paths.
- Filter to Carta's doc scope via the existing `_in_doc_scope`.
- Read each path's content **at the range tip** via `git show <tip>:<path>`,
  where `tip` is the right operand of the range (`A..B` / `A...B` → `B`;
  a trailing-empty right side, e.g. `A...`, → `HEAD`).
- Fails open per file (a `git show` error skips that file).

The slice-1 `collect_pushed` already contains this exact inner loop (diff a
range, scope-filter, `git show <tip>:<path>`, dedupe). Factor that loop into a
shared private helper (e.g. `_collect_range_docs(repo_root, cfg, rng, tip)`) and
have both `collect_pushed` and `collect_range` call it, so the two collectors do
not duplicate logic. This refactor must keep all existing slice-1 collector tests
green (behavior-preserving).

### Reused unchanged

`run_stale_scan`, `_stale_judge` / `ollama_yesno`, the `hooks.stale_scan` config
block (threshold, judge model, timeout, `max_judge_calls`, `block_on_stale`), and
the `_print_stale_result` plain reporter in `carta/cli.py`. No new config keys; no
new module; no change to `sections_from_markdown`, `chunk_text`, or `StaleFinding`.

### `cmd_hook` check-path changes

In the `action == "check"` branch of `cmd_hook`:

1. Resolve config and `repo_root` as today (fail open on missing config → exit 0; disabled → exit 0).
2. Collection:
   - if `args.diff` is set → `range_spec = <default-branch>...HEAD` when the value
     signals "use default", else the explicit value; `docs = stale_scan.collect_range(repo_root, cfg, range_spec)`.
   - else → existing staged / pushed collection by stage.
   - Collection errors fail open (exit 0), as in slice 1.
3. No docs → exit 0.
4. `result = stale_scan.run_stale_scan(repo_root, cfg, docs)` (fail open → exit 0 on error).
5. `_print_stale_result(result, scfg)` (plain, to stderr).
6. Exit 1 if `result.findings` **and** (`args.fail_on_stale` **or** `scfg["block_on_stale"]`); else exit 0.

Collectors/scan stay module-qualified (`stale_scan.collect_range(...)`,
`stale_scan.run_stale_scan(...)`) so tests can monkeypatch them, consistent with
slice 1.

## Out of scope (other #10 slices / deferred)

- GitHub `::warning::` annotations and any `--report` format (deferred; revisit only if a graph-accessible runner is ever set up).
- PR-comment posting via the GitHub API.
- A sample/committed CI workflow, and any `carta export`/import-into-CI tooling.
- Line-number / heading-line mapping of findings (only needed for annotations).
- Scanning changed code/config (vs docs).
- Query-time agent context hints (slice 3) and the expanded CLAUDE.md `/doc-search` block (slice 4).

## Acceptance

- `carta hook check --diff origin/main...HEAD` scans the docs changed on the
  branch vs the given base and warns on superseded sections, using the live graph.
- `carta hook check --diff` (no value) uses the `<default-branch>...HEAD` range.
- With no `--diff`, `check` behaves exactly as slice 1 (no regression).
- A run with findings exits 0 by default and exits 1 under `--fail-on-stale`
  (or `block_on_stale: true`).
- The scan fails open on any infrastructure error (no config, Qdrant/Ollama
  unreachable, bad range) — never a non-zero exit from a transient failure.
- `collect_range` and `collect_pushed` share one collection loop; all slice-1
  collector tests remain green.
- All new behaviour is covered by tests written test-first, and the full suite
  stays green.

## Testing approach

- `collect_range` against a real temp git repo: a two-commit range surfaces the
  changed doc at the tip's content; non-doc and out-of-scope paths are excluded;
  an explicit `A..B` range and the default `<branch>...HEAD` range both work.
- The shared-loop refactor: existing `collect_pushed` tests stay green unchanged.
- `cmd_hook` with `--diff`: monkeypatch `stale_scan.collect_range` /
  `run_stale_scan`; assert it collects via the range path and prints findings.
- Exit codes: findings + `--fail-on-stale` → exit 1; findings without it → exit 0;
  no findings → exit 0; default-range resolution when `--diff` has no value.
- Argparse: `--diff` optional-value parsing (absent vs bare vs explicit).
