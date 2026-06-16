---
id: 2026-06-16-claude-md-doc-search-guidance-design
title: CLAUDE.md /doc-search guidance block in bootstrap
status: shipped
related: [2026-06-15-stale-reference-git-hook-design, 2026-06-16-stale-reference-diff-scan-design]
date: 2026-06-16
related_issue: Ian-q/Carta#10
---

# CLAUDE.md /doc-search guidance block (#10, slice 4 of 4)

## Background

Issue #10's fourth mechanism is "improved CLAUDE.md anchor-doc guidance": make it
obvious to every agent — without prior context — that a queryable knowledge graph
exists, **when** to search it, and the exact skill to invoke (`/doc-search`). The
stale-reference detectors (slices 1-2) are only half the story; an agent also needs
to *proactively* search before assuming it knows the current spec.

Today `carta init`'s `_append_claude_md(project_root, project_name)` in
`carta/install/bootstrap.py` appends a single HTML comment to an existing CLAUDE.md:

```
<!-- Carta is active. Collections: <p>_doc, <p>_session, <p>_notes -->
```

guarded by a `"Carta is active"` substring check, and **only if CLAUDE.md already
exists** (no file → no write). This slice expands that into a real, on-by-default
`/doc-search` guidance block.

Slice 3 (query-time context hints) needs no work: the existing `UserPromptSubmit`
proactive-recall hook already injects scored doc hits on every prompt through a
three-zone relevance gate. This slice (4) is the last of #10.

## Decision summary

- **On by default.** The block is part of what `carta init` writes — no config gate
  (matches issue #10's acceptance: everything off-by-default *except* the CLAUDE.md block).
- **Create-if-absent.** If the repo has no CLAUDE.md, create a minimal one (`# <project>`
  heading + the block). If it exists, append the block.
- **Full guidance block**, refined by a `claude-md-improver` pass: lead with the
  imperative, a trigger→query table (no duplicated "examples"), and demoted maintenance
  commands so `/doc-search` stands alone.
- **Idempotent.** Skip if the block's start marker *or* the legacy `"Carta is active"`
  string is already present (re-running `carta init`, or upgrading a repo that has the
  old one-liner, never double-injects).
- **Generic content.** No project-specific examples (the issue's Teensy/CAN specifics
  are dropped); categories are component/API/config/path/design-decision.
- **Does not** retro-edit this Carta repo's own CLAUDE.md — only future `carta init` runs.

## Design

### The injected block

`_carta_claude_block(project_name: str) -> str` returns (pure function, testable):

```markdown
<!-- carta:guidance:start -->
## Carta Knowledge Graph

**Search the docs before you assume.** This project's specs (components, protocols,
config keys, design decisions) may have changed since the code — or your training — was
written. Carta provides semantic search over them via `/doc-search`.

**Run `/doc-search` whenever a prompt names one of these — query it like so:**

| Prompt mentions… | Search |
|---|---|
| A component or module | `/doc-search "<name> responsibilities"` |
| An API, protocol, or data format | `/doc-search "<name> spec"` |
| A config key or flag | `/doc-search "<key> configuration"` |
| A file/path or a design decision | `/doc-search "<topic> design"` |

Maintenance (only when asked, or after editing docs): `/doc-audit` (flag
stale/contradictory docs), `/doc-embed` (re-index).

<!-- Carta is active. Collections: {project_name}_doc, {project_name}_session, {project_name}_notes -->
<!-- carta:guidance:end -->
```

`{project_name}` is interpolated. The `Carta is active. Collections:` comment is kept
**inside** the block (preserves the existing diagnostic breadcrumb and the legacy
idempotency string).

### `_append_claude_md` rework

```
SENTINEL = "<!-- carta:guidance:start -->"
LEGACY   = "Carta is active"

def _append_claude_md(project_root, project_name):
    claude_md = project_root / "CLAUDE.md"
    block = _carta_claude_block(project_name)
    if claude_md.exists():
        text = claude_md.read_text()
        if SENTINEL in text or LEGACY in text:
            return                      # idempotent — already injected (new or legacy)
        with open(claude_md, "a") as f:
            f.write("\n" + block)
    else:
        claude_md.write_text(f"# {project_name}\n\n{block}")
```

- Existing CLAUDE.md without Carta → append the block (leading newline for separation).
- Existing CLAUDE.md with the new marker OR the legacy `"Carta is active"` line → no-op.
- No CLAUDE.md → create `# <project_name>\n\n<block>`.

### Constraints / interfaces

- `_carta_claude_block` is the single source of the block text; `_append_claude_md`
  is the only writer. One responsibility each.
- No new config keys. `anchor_doc` (default `"CLAUDE.md"`) is unchanged; this slice
  keeps the hard-coded `CLAUDE.md` target the current code already uses (honoring a
  custom `anchor_doc` path is out of scope — see below).

## Out of scope

- Honoring a non-default `anchor_doc` path (the current code already hard-codes
  `CLAUDE.md`; not regressing, not extending).
- Config-gating the block (issue says it is the one on-by-default behavior).
- Editing the existing AGENTS.md generator (`_create_agents_md`) — separate artifact.
- Retro-editing this Carta repo's own CLAUDE.md.
- Upgrading a repo's pre-existing legacy one-liner to the full block (treated as
  "already injected" → skipped, to avoid mangling user CLAUDE.md files).

## Acceptance

- After `carta init` in a repo **with** a CLAUDE.md lacking Carta content, the file
  gains the full `## Carta Knowledge Graph` block (markers, trigger table, `/doc-search`
  primary, collections comment).
- After `carta init` in a repo **without** a CLAUDE.md, a new CLAUDE.md is created with
  a `# <project>` heading and the block.
- Re-running `carta init` does not duplicate the block (start marker present → skip).
- A repo whose CLAUDE.md already has the legacy `"Carta is active"` one-liner is left
  unchanged (no second block appended).
- The interpolated collections comment names `<project>_doc/_session/_notes`.
- All new behaviour is covered by tests written test-first; the full suite stays green.

## Testing approach

- `_carta_claude_block("acme")` returns text containing the start/end markers,
  `## Carta Knowledge Graph`, the `/doc-search` trigger table, the maintenance line,
  and `acme_doc`/`acme_session`/`acme_notes` in the collections comment.
- Append path: a tmp CLAUDE.md with prior content gains the block exactly once; prior
  content is preserved.
- Create path: no CLAUDE.md → file created with `# <project>` heading + block.
- Idempotency: second `_append_claude_md` call (new marker present) → no change;
  a CLAUDE.md seeded with only the legacy `"Carta is active"` line → no change.
- Preserve/adjust any existing `_append_claude_md` test that asserted the old
  one-line-only behavior (update it to expect the block while keeping the
  `Carta is active. Collections:` substring assertion valid).
