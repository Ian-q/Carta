---
status: resolved
trigger: "Two regressions found in doc-audit-cc 0.1.6: carta search silent empty output, changed_since_last_audit empty on first run"
created: 2026-03-24T00:00:00Z
updated: 2026-03-24T01:00:00Z
---

## Current Focus

hypothesis: Both bugs confirmed and fixed.
test: run existing test suite
expecting: all tests pass (they do — 81/81)
next_action: await human verification in real install environment

## Symptoms

expected: carta search returns results or clear "no results" message; changed_since_last_audit returns files changed since last audit (or repo init if no prior audit)
actual: carta search produces NO output at all; changed_since_last_audit returns []
errors: 0.1.5 crashed with "'QdrantClient' object has no attribute 'search'" — 0.1.6 fix may have swallowed the error
reproduction: Run `carta search "test"` after carta init; run first audit in fresh repo
started: Regression in 0.1.6

## Eliminated

- hypothesis: scanner.py changed between 0.1.5 and 0.1.6
  evidence: git diff 901fc81 d619254 -- carta/scanner/scanner.py produces no output — scanner is identical
  timestamp: 2026-03-24

- hypothesis: changed_since_last_audit empty is a regression introduced in 0.1.6
  evidence: scanner code unchanged; bug is a pre-existing design flaw — first-run fallback only includes tracked_docs (docs_root), not all git-tracked .md files
  timestamp: 2026-03-24

## Evidence

- timestamp: 2026-03-24
  checked: carta/embed/pipeline.py run_search() lines 167-175
  found: query_points() exception is caught, warning printed to stderr, empty list returned silently
  implication: cmd_search iterates empty list and prints nothing — no stdout output at all

- timestamp: 2026-03-24
  checked: carta/cli.py cmd_search() lines 69-71
  found: no empty-result check before the for-loop; empty results = zero print calls = silent exit 0
  implication: user and LLM skill both see no output; no way to distinguish empty-collection from broken

- timestamp: 2026-03-24
  checked: carta/scanner/scanner.py run_scan() lines 552-555 (original)
  found: first-run fallback was `[str(p.relative_to(repo_root)) for p in tracked_docs]` — tracked_docs is only docs_root/*.md
  implication: repos with no docs/ folder or with .md files outside docs/ (CLAUDE.md, firmware READMEs) get changed=[] on first run

- timestamp: 2026-03-24
  checked: 0.1.5 test result showing ['CLAUDE.md', 'firmware/sdd-v2-arduino/README.md']
  found: those files are outside docs_root — they came from git diff (previous_hash was set), not from tracked_docs fallback
  implication: confirms first-run fallback was always wrong for repos without docs/; 0.1.5 appeared to work because run was not a true first run

## Resolution

root_cause: |
  Bug 1: cmd_search() has no empty-result guard. run_search() can return [] either because
  the collection is empty OR because query_points() threw an exception (swallowed with stderr warning).
  Empty list → for-loop prints nothing → complete silence.

  Bug 2: run_scan() first-run fallback (no previous_hash) built changed_since_last_audit from
  tracked_docs, which only contains files inside docs_root/. Repos without a docs/ directory
  or with .md files outside it (CLAUDE.md, firmware READMEs) produced changed=[].

fix: |
  Bug 1: Added `if not results: print("No results found."); return` in cmd_search() before the
  for-loop. carta/cli.py lines 70-72.

  Bug 2: Replaced tracked_docs fallback with `git ls-files` on first run, filtering for .md and
  .embed-meta.yaml, applying excluded_paths. This covers all git-tracked files repo-wide.
  carta/scanner/scanner.py — added get_initial_commit_hash(), rewrote else-branch of changed_since block.

verification: 81/81 tests pass (pytest carta/scanner/tests/ carta/embed/tests/)
files_changed:
  - carta/cli.py
  - carta/scanner/scanner.py
