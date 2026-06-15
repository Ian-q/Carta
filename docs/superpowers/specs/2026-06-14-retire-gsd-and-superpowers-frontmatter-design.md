---
id: 2026-06-14-retire-gsd-and-superpowers-frontmatter-design
title: Retire GSD machinery + add frontmatter to the superpowers doc corpus
status: shipped
related: []
date: 2026-06-14
related_issue: Ian-q/Carta#46, Ian-q/Carta#48
---

# Retire GSD machinery + add frontmatter to the superpowers doc corpus

Cleanup of two open issues that both trace back to doc-audit #1. They are
independent and ship as **two PRs**.

## Background

- **#46** — `.planning/codebase/` docs and the GSD-generated blocks embedded in
  `CLAUDE.md` are frozen at 2026-03-25. They describe a four-command CLI,
  colocated sidecars, and a `_quirk` collection — none of which match the
  current code. The GSD planning plugin is no longer used, so these blocks will
  never be regenerated; they only rot. **Decision: retire, do not regenerate.**
- **#48** — Most docs under `docs/superpowers/` lack YAML frontmatter, so
  `carta scan` reports `stats.with_id: 0` repo-wide. Cross-reference tracking and
  stale-doc detection can't work without it.

## Issue #46 — Retire GSD  (branch `chore/retire-gsd`)

### Changes

1. **Delete `.planning/` entirely** (`git rm -r .planning/`). It holds the stale
   GSD `codebase/` docs plus phase plans, resolved todos, research, and debug
   notes. All of it is GSD-era history; the live design record now lives in
   `docs/superpowers/`. The directory is already excluded from carta
   scanning/embedding, so nothing downstream depends on it.

2. **De-GSD-ify `CLAUDE.md`:**
   - Strip all five `<!-- GSD:* -->` marker pairs.
   - **Keep** (promoted to hand-maintained): the Project intro, Technology
     Stack, and Conventions sections — accurate and largely timeless.
   - **Delete** the *Architecture* block. It is the stale one (colocated
     sidecars, four-command CLI, `_quirk`) and is already superseded by the
     hand-maintained "Carta surface — authoritative reference" section.
   - **Delete** the *Developer Profile* block — a `/gsd:profile-user`
     placeholder for a plugin we no longer run.
   - Rewrite the "Carta surface" note so it no longer points at "the
     GSD-generated blocks above … see #46". It becomes simply: hand-maintained,
     authoritative for the current surface.
   - Simplify the Superpowers workflow note (drop "not GSD" — GSD is gone).
   - Fix the trailing Carta-active comment: `doc-audit-cc_quirk` →
     `doc-audit-cc_notes`. (Source generator in `bootstrap.py` already emits
     `_notes`; this line is a stale leftover — docs-only fix, no code change.)

3. Leave `.planning/` in the exclude lists (`carta/config.py` defaults and
   `.carta/config.yaml`) — harmless defensive no-ops; trimming them is out of
   scope.

### Acceptance

No GSD markers or machinery remain in the repo; no doc references colocated
sidecars, a four-command CLI, or a `_quirk` collection.

## Issue #48 — Frontmatter for `docs/superpowers/`  (branch `docs/superpowers-frontmatter`)

### Scope

23 specs (4 already carry partial frontmatter), 23 plans (none), 3 skill docs
(already carry `name`/`description`). ~42 files get new frontmatter; the 4
partial specs are normalized to the shared schema.

### Template

```yaml
---
id: <filename-stem>          # stable, unique, greppable
title: <H1 of the doc>
status: shipped | superseded | draft
related: [<paired doc id>]   # spec <-> plan by shared slug; [] if none
date: YYYY-MM-DD             # filename date prefix, else git first-commit date
---
```

- **`id`** — the filename stem (without `.md`). Stable, unique, no judgment.
- **`title`** — the document's H1; fall back to a title-cased slug.
- **`status`** — **verified against the current code/CLI**, not guessed:
  - `shipped` — the feature is verifiably present in the code or CLI surface.
  - `superseded` — replaced by a later design/plan.
  - `draft` — explicitly a draft, or designed-but-not-implemented.
- **`related`** — spec ↔ plan pairing by shared slug
  (e.g. `…-audit-command-design` ↔ `…-audit-command-implementation`). Roughly
  15 clean pairs; the remainder are unpaired (`related: []`).
- **`date`** — the `YYYY-MM-DD` filename prefix where present, otherwise the
  file's first git-commit date.

### Skill docs

The three files under `docs/superpowers/skills/` already have functional
`name`/`description` frontmatter. Add `id` and `status: shipped` while preserving
the existing keys; do not disturb `name`/`description`.

### Execution

The files are independent, so dispatch parallel subagents. Each agent receives
the template, the spec↔plan pairing map, and the status rubric, handles a batch,
and **verifies each file's status by grepping the code** before writing
frontmatter.

### Acceptance

`carta scan` reports `with_id > 0`; agents can grep `status: shipped` to find
authoritative docs.

## Out of scope

- Regenerating any planning docs (we retire instead).
- Removing `.planning/` from exclude lists.
- Touching the active skill definitions under the repo-root `skills/` tree.
