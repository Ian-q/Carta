# CLAUDE.md ↔ docs sync — design

**Status:** approved design (brainstorming complete)
**Date:** 2026-06-26
**Working names:** skill `/claude-md-sync`, CLI `carta claude-md`

## Problem

CLAUDE.md is the highest-leverage doc in the repo: Claude Code injects it verbatim
as *authoritative* project instructions into every session and every subagent. A stale
paragraph in `docs/architecture.md` sits idle until someone reads it; a stale claim in
CLAUDE.md actively misleads every future agent — it has the blast radius of a system
prompt.

When a project makes a big change, the docs get updated, but CLAUDE.md — a hand-curated
digest of systems, conventions, and a command surface — silently drifts out of sync.
Carta already keeps docs consistent *with each other* (the stale-reference scan); it does
not yet keep CLAUDE.md consistent with the docs. This design closes that gap.

## Goal

When the docs change (and are re-embedded), detect the CLAUDE.md sections the updated docs
have **superseded**, and have the **in-session agent draft the corrections** — human-gated.
Detection is local and programmatic (the ≤2B Ollama judge); writing anything that lands in
a permanent doc is delegated to the capable in-session model, which holds the full context
of what just changed.

This is option **B1** from brainstorming: Carta is the detector and evidence-provider; the
agent is the author; the human approves.

## Key decisions (from brainstorming)

- **Direction:** docs are the source of truth; CLAUDE.md is the derivative that follows them.
- **Output:** mostly B (draft the fix), via B1 — the agent drafts, Carta never auto-writes.
- **Trigger:** primarily a **skill the agent invokes near end-of-session**, when it still
  holds the context of the changes it made. The pre-PR git hook is demoted to a one-line
  *nudge*, not the main event.
- **Directive protection (S1):** do **not** pre-classify sections. Lean on the supersession
  judge's conservatism plus the human gate. Durable directives ("use TDD", "Ollama judge
  ≤2B params", "snake_case") have no doc in the graph that supersedes them, so they are not
  flagged in the first place; the rare false positive is cheap because the agent reviews
  every draft and the human approves it. A `pinned` escape hatch is reserved in the sidecar
  schema for any directive that nonetheless keeps getting false-flagged.
- **Metadata home:** a **sidecar**, never the file. Anything written into CLAUDE.md itself —
  frontmatter included — is injected into every session; frontmatter is *prime real estate*
  at the top of the file and would pollute context worse than scattered inline comments.
  Per-section sync metadata lives out-of-band in `.carta/sidecars/CLAUDE.md.sync.yaml`,
  consistent with Carta's existing sidecar pattern, at zero context cost.

## Non-goals (deliberate v1 scope boundaries)

- **No code-reference drift detection.** A cited `embed.py:17`, a renamed command, or a
  changed default value that the *code* changed but no doc mentions is a *different*
  detector — the graph holds docs, not code. Out of v1. The "up to date with **docs**"
  framing already excludes it. (Candidate future work.)
- **No auto-rewrite.** The agent drafts; the human approves. Carta never writes CLAUDE.md.
- **CLAUDE.md is never embedded as an authoritative graph source** — only ever a scan
  *target*. If it were embedded, it could appear as the passage "superseding" a real doc,
  which is backwards.

## Architecture

Mostly reuse of `carta/hook/stale_scan.py::run_stale_scan`, which already does the core
loop: section a changed doc → search the graph per section → ask the ≤2B judge whether the
section was superseded → collect findings. Four small additions plus a skill and an optional
hook nudge.

### Component 1 — scan-target generalization (`carta/hook/stale_scan.py`)

Today `_in_doc_scope` requires a `.md` file under `docs_root`, so CLAUDE.md (repo root) is
out of scope. Add an explicit-target path so CLAUDE.md can be fed in as a
`ChangedDoc(path="CLAUDE.md", text=<current content>)` and scanned by the same engine,
bypassing the `docs_root` filter for this one known target.

CLAUDE.md remains a scan *target* only — it is not embedded as a graph source. The existing
`hits = [h for h in hits if h.get("source") != doc.path]` self-filter already prevents a doc
from superseding itself; the additional guarantee here is that CLAUDE.md is never indexed
into the `*_doc` collection in the first place.

### Component 2 — enrich `StaleFinding` for drafting (`carta/hook/stale_scan.py`)

`StaleFinding` currently carries `candidate_path` and `candidate_score` plus a 160-char
`snippet`. To let the agent draft a correction it needs the actual material:

- `candidate_excerpt` — the superseding passage's text (the hit's `excerpt`).
- the **full section text** and **section heading** — not just the truncated snippet.

Findings are produced at chunk granularity (see Component 3) and then grouped up to their
nearest `##`/`###` heading so the agent rewrites coherent sections, not fragments.

### Component 3 — section granularity / grouping

`chunk_text` splits large sections (the "Carta surface" command table, the long Conventions
block) into ~`max_tokens` (~400) chunks, so each chunk is searched and judged independently.
For the sync output, group chunk-level findings back up to their owning top-level heading so
a finding reads as "heading X is superseded by docs A and B," with all superseding excerpts
attached. This grouping is the main piece of *new* logic beyond plumbing.

The ~400-token default is inherited from `embed.chunking.max_tokens`. We may need to tune
granularity higher or lower after testing on a real CLAUDE.md; tracked as a follow-up issue
(see "Follow-ups").

### Component 4 — sync sidecar (`.carta/sidecars/CLAUDE.md.sync.yaml`)

A new, out-of-band metadata file, distinct from the `.embed-meta.yaml` sidecars (CLAUDE.md is
not embedded). Mirrors Carta's sidecar conventions and is **never injected into a session**.

```yaml
schema: 1
last_synced: 2026-06-26T00:00:00Z
sections:
  "## Constraints":
    hash: "<sha256 of section text>"
    pinned: true            # S3 escape hatch — scan skips pinned sections
    last_reviewed: 2026-06-26T00:00:00Z
  "### Carta surface — authoritative reference":
    hash: "<sha256 of section text>"
    pinned: false
    last_reviewed: 2026-06-26T00:00:00Z
```

- **`hash`** is part of a *safe* skip rule, not the whole of it. Staleness is triggered by
  the **docs** changing, not by CLAUDE.md changing — so a section may be freshly superseded
  even though its own text is untouched. A section is therefore skipped only when **both**
  hold: (1) its text hash matches `hash`, **and** (2) no doc has been re-embedded since this
  section's `last_reviewed` (a graph-unchanged guard, e.g. via the max `indexed_at` across
  the embed sidecars). If either side changed, the section is re-scanned. Default behaviour
  with no usable graph-change signal is to **scan all unpinned sections** — correctness over
  speed, since a missed CLAUDE.md supersession is the expensive failure.
- **`pinned`** is the directive escape hatch — a pinned section is skipped by the scan
  entirely. Reserved in the schema; enforcement is a tiny conditional and ships in v1.
- Keyed by section heading. The scan re-sections CLAUDE.md anyway, so re-location is nearly
  free; a renamed heading simply reverts that section to unpinned/unhashed, which is safe
  (warn-only, human-gated).

### Component 5 — CLI `carta claude-md check`

Runs the scan over CLAUDE.md and emits **structured JSON findings** for the skill to consume:

```json
{
  "scanned": true,
  "findings": [
    {
      "heading": "### Carta surface — authoritative reference",
      "section_text": "<full current text of the CLAUDE.md section>",
      "superseding": [
        {"source": "docs/architecture/retrieval.md", "excerpt": "<authoritative passage>", "score": 0.81}
      ]
    }
  ],
  "skipped_pinned": 2,
  "skipped_unchanged": 9,
  "judge_calls": 4
}
```

Reads the sidecar first (skip pinned, skip hash-unchanged), runs the scan on the rest, groups
findings by heading, and reports counts. Reuses `hooks.stale_scan.*` config
(`candidate_threshold` 0.65, `max_judge_calls` 30, `ollama_model`, `judge_timeout_s`).

### Component 6 — skill `/claude-md-sync` (`carta/skills/claude-md-sync/SKILL.md`)

The agent-facing end-of-session entry, mirroring `/doc-embed`'s shape. Steps:

1. Run `carta claude-md check`, capture the JSON findings.
2. If no findings: report "CLAUDE.md is in sync" and stop.
3. For each flagged heading, **draft a corrected section** using the superseding excerpts
   *and the agent's full session context* about what changed.
4. Present a diff of each proposed change to the user.
5. On approval, edit CLAUDE.md; on rejection, leave it untouched.
6. Write back the sidecar: update `hash` and `last_reviewed` for reconciled sections, set
   `last_synced`, preserve `pinned` flags.

### Component 7 — hook nudge (optional, pre-PR)

The existing `carta hook check` pre-PR path gains a single warn line when CLAUDE.md has
suspected-stale sections — e.g. "CLAUDE.md may be stale vs N doc(s); run /claude-md-sync".
No drafting, no blocking. This is a nudge toward the skill, consistent with the rest of the
warn-only, fail-open hook behavior.

## Data flow (end-of-session)

```
agent finishes work
  → docs edited & `carta embed`'d (graph now reflects new truth)
  → agent invokes /claude-md-sync
    → CLI `carta claude-md check`
      → read sidecar; skip pinned; skip sections whose text hash is unchanged
        AND whose graph is unchanged since last_reviewed (else re-scan)
      → section remaining CLAUDE.md (sections_from_markdown + chunk_text)
      → per chunk: search graph (text-only cfg) → judge supersession
      → group chunk findings by heading; attach superseding excerpts
      → emit JSON
    → agent drafts corrected text per heading (full session context)
    → present diff → user approves
    → agent edits CLAUDE.md; skill writes back hashes / last_synced
```

## Error handling — fails open everywhere

Matches the existing stale_scan + judge conventions:

- Judge returns `None` (network/parse error) → section not flagged.
- Search raises → skip that section.
- Missing or corrupt sidecar → treat every section as unpinned/unhashed.
- `max_judge_calls` cap reached → remaining sections reported as skipped-overflow, not failed.
- Nothing ever blocks, and **nothing auto-writes CLAUDE.md** — every change is agent-proposed
  and human-approved.

## Testing (TDD)

All core logic lives in testable Python (CLI + modules); the skill is thin orchestration —
same discipline as the `carta_focus` `add_tool` note. `run_stale_scan` already accepts
injectable `search_fn` / `judge_fn`, so tests need no live Qdrant/Ollama.

- CLAUDE.md comes into scan scope via the explicit-target path.
- `StaleFinding` carries `candidate_excerpt` + full section text + heading.
- Chunk-level findings group correctly up to their owning `##`/`###` heading.
- Sidecar round-trips: read/write, pin-skip of pinned sections, preservation of `pinned`
  across writes, and the safe skip rule — a hash-unchanged section is **still re-scanned**
  when a doc was embedded after its `last_reviewed`, and is only skipped when both text and
  graph are unchanged.
- CLAUDE.md is never written into the embed collection (target-not-source guarantee).
- Fail-open paths: `None` judge, search exception, missing sidecar — no finding, no crash.

## Follow-ups

- **GitHub issue [#81](https://github.com/Ian-q/Carta/issues/81):** tune section
  granularity (`max_tokens` / grouping) after testing on a real CLAUDE.md; keep the
  ~400-token default for now.
- **Future detector (separate spec):** code-reference drift — verify cited `file:line`,
  command names, and default values against the actual code, not the doc graph.
