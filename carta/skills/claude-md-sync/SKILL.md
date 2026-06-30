---
name: claude-md-sync
description: Reconcile CLAUDE.md against the docs after a change. Detect sections the updated docs have superseded and draft corrections, human-gated. Use near end-of-session once docs are edited and embedded.
---

# /claude-md-sync Skill

Keep CLAUDE.md — which every session and subagent loads as authoritative — in sync with the
docs after a change. Carta detects the stale sections; **you** draft the fixes using the full
context of what changed this session; the human approves. Carta never edits CLAUDE.md.

Run this near the **end of a session**, after the docs are edited and re-embedded
(`carta embed`), so the graph reflects the new truth.

---

## Step 1: Detect superseded sections

```bash
carta claude-md check
```

> Uses the installed `carta` CLI. If it isn't on PATH, run `python -m carta claude-md check`.

Parse the JSON. If `findings` is empty, report "CLAUDE.md is in sync with the docs." and stop.
Each finding has:

- `heading` — the CLAUDE.md section heading
- `section_text` — the full current text of that section
- `superseding` — `[{source, excerpt, score}]`: the doc passages that replaced it

`skipped_pinned` / `skipped_unchanged` are informational (pinned or already-reconciled sections).

**Heed `judge_errors`.** If `judge_errors` > 0, that many judge calls timed out or failed — those
sections were *not actually evaluated*, and a non-responding judge reads as "not superseded." So a
`0 findings` result with `judge_errors` > 0 is **not** a clean bill of health. Tell the user the
judge is timing out and to raise `hooks.stale_scan.judge_timeout_s` (and/or check the Ollama judge
model) before trusting the result.

---

## Step 2: Draft a correction per finding

For each finding, draft replacement text for `section_text` that reflects the `superseding`
excerpts **and** your session context. Hold the line on what CLAUDE.md is for:

- **Only correct descriptive claims** about systems, commands, and structure.
- **Never "correct" a durable directive or convention** ("use TDD", "Ollama judge ≤2B params",
  naming rules) toward what the code happens to do — those are the source of truth, not the docs.
  If a finding targets a directive, treat it as a false positive and skip it (optionally tell the
  user it can be pinned in `.carta/sidecars/CLAUDE.md.sync.yaml`).

---

## Step 3: Present diffs and get approval

Show each proposed change as a before/after diff. Apply **only** the changes the user approves;
edit CLAUDE.md with the Edit tool. Leave rejected sections untouched.

---

## Step 4: Record the sync

After edits are applied (or if the user approved no changes but you want to checkpoint the
current state), finalize the sidecar so unchanged sections skip next time:

```bash
carta claude-md record
```

---

## Completion

Report a summary:

> "CLAUDE.md sync complete. N sections flagged, M corrected and approved, K skipped. Sidecar recorded."
